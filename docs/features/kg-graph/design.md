# Feature design — Knowledge Graph (`kg.json`)

> Status: agreed (grilled 2026-06-10). Implements the navigation index over the
> experience ledger. Decisions below are locked; schema changes go through
> DESIGN.md (§7.3 extension + new §7.6).

## 1. Problem

The experience ledger already contains everything the proposer needs — but the
"what changed → what happened" relationships are *implicit*, scattered across
`design_spec.json`, `result.json`, and `hypothesis.md` front-matter in N bundle
directories. The agent must open many files to reconstruct lineage and
parameter→score effects. We materialize that lineage as a small, derived,
machine-built knowledge graph the agent can `Read` in one shot, then follow
pointers into the full bundles.

## 2. Locked decisions (from the grilling session)

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Source of truth | KG is **purely derived** from bundles by deterministic engine code. The LLM never writes it. Bundles remain the single source of truth; `kg.json` is an index. |
| 2 | Edge content | **Facts + pointers only**: `param_diff`, `score_delta`, endpoint statuses, `axis` label. No prose, no `expected`, no hypothesis-held judgment — interpretation lives in `hypothesis.md`, which the agent reads via the pointer. (Meta-Harness: never replace raw experience with summaries; an index of facts is not a summary.) |
| 3 | Node granularity | **Candidates only** (no parameter/objective nodes). Query-by-parameter = scan edges' `param_diff` keys; trivial for an LLM at this scale. |
| 4 | Multi-parent lineage | hypothesis hand-off (§7.3) gains optional `inspired_by: [names]`. Two edge kinds: `mutated-from` (single primary parent, carries diff/delta) and `inspired-by` (pure lineage pointer, no diff). |
| 5 | Storage / rebuild | Persistent `run_dir/kg.json`, **fully rebuilt** on every eval inside `_finalize()` *before* the commit (same recompute-from-ledger philosophy as `frontier.json`). Incremental update rejected: rebuild is self-healing, O(N) over a few hundred tiny JSON files (milliseconds), and the full builder must exist anyway for backfill. |
| 6 | Query interface | Agent `Read`s `kg.json` directly (same access pattern as `frontier.json`). New CLI subcommand `meta-research kg` only rebuilds + prints a summary (backfill for pre-KG experiment dirs). No filter flags in v1. |

Self-resolved details:

- **Diff semantics**: shallow top-level diff of `DesignSpec.params` —
  `changed: {key: [old, new]}`, `added: {key: new}`, `removed: {key: old}`.
  Non-scalar values are compared as opaque wholes. (Grounded in the example:
  params are flat dicts; different designs legitimately have different key sets.)
- **`score_delta`**: per-objective `child − parent` over the child's score keys;
  `null` where the parent lacks the key (e.g. crashed parent).
- **Parent resolution**: `parent`/`inspired_by` are candidate *names*; resolve to
  the **latest bundle of that name with iteration < child's iteration**.
  Unresolvable names append a human-readable string to `warnings` (never silent,
  never fatal).
- **Crashed / infeasible candidates are nodes too** (store everything; `status`
  marks them). Diffs stay mechanical even when a crashed child has empty params.
- **No git SHA on nodes**: the KG is rebuilt before the commit exists; the bundle
  path is the pointer.
- **Failure isolation**: a KG rebuild failure must not sink the eval step —
  attach `{"kg_error": repr(exc)}` to result metadata (same pattern as
  `frontier_error`).
- **Determinism**: nodes sorted by `(iteration, name)`; edges sorted by
  `(dst, kind, src)`; JSON written with `sort_keys=True`.
- **`inspired_by` persistence**: the KG builder reads bundles only, so the
  hand-off value must be persisted into `hypothesis.md` front-matter as a
  comma-separated scalar (`inspired_by: a, b`), parsed back by the line-based
  front-matter reader. Absent/empty ⇒ no `inspired-by` edges.
- **`meta-research init --from`**: add `kg.json` to `_EXAMPLE_IGNORE` (generated
  artifact, like `frontier.json`/`results.tsv`).

## 3. `kg.json` schema (DESIGN.md §7.6)

```json
{
  "version": 1,
  "nodes": [
    {
      "id": "012_staggered_pin_v3",
      "iteration": 12,
      "name": "staggered_pin_v3",
      "status": "frontier",
      "scores": {"thermal_resistance": 0.042, "pressure_drop": 820.0},
      "bundle": "experience/012_staggered_pin_v3"
    }
  ],
  "edges": [
    {
      "kind": "mutated-from",
      "src": "001_pin_fins",
      "dst": "012_staggered_pin_v3",
      "axis": "flow_arrangement",
      "param_diff": {
        "changed": {"arrangement": ["inline", "staggered"]},
        "added": {},
        "removed": {}
      },
      "score_delta": {"thermal_resistance": -0.019, "pressure_drop": 230.0}
    },
    {"kind": "inspired-by", "src": "000_straight_fins", "dst": "012_staggered_pin_v3"}
  ],
  "warnings": ["012_staggered_pin_v3: parent 'pinfins' not found in experience/"]
}
```

- `id` == bundle directory name (`<iter:03d>_<name>`); `bundle` is the path
  relative to `run_dir`.
- `src` is the parent/inspiration node id, `dst` the child.
- `axis` is copied from the child's front-matter (label, not a conclusion).
- A node with `parent: ""` (baselines) simply has no `mutated-from` edge.

## 4. Module design

New file `meta_research/kg.py` (engine, deterministic, no third-party imports):

```python
KG_FILENAME = "kg.json"
KG_VERSION = 1

def param_diff(old: dict, new: dict) -> dict        # {"changed","added","removed"}
def score_delta(child: dict, parent: dict) -> dict  # child keys; None where parent lacks
def build_kg(run_dir: Path) -> dict                 # pure: bundles -> graph dict
def write_kg(run_dir: Path) -> Path                 # build + write kg.json, return path
```

`build_kg` reads, per bundle: `design_spec.json` (params), `result.json`
(scores/status), `hypothesis.md` front-matter (`parent`, `axis`, `inspired_by`)
— reusing `experience._read_hypothesis` / constants. Malformed bundles are
skipped with a warning entry (mirrors `Experience.history()` tolerance).

## 5. Integration points

1. `experience.py`: `_render_hypothesis_md` writes an `inspired_by:` front-matter
   line (comma-separated; `""` when absent); `_read_hypothesis` already returns
   raw front-matter keys — no change needed for reading.
2. `runner._finalize()`: after `update_frontier`, before the `results.tsv`
   append/commit — `write_kg(run_dir)` guarded like `update_frontier`
   (failure ⇒ `kg_error` metadata).
3. `cli.py`: `meta-research kg [--run-dir DIR]` — calls `write_kg`, prints node /
   edge / warning counts. Does **not** load `prepare.py` (pure ledger read,
   like `frontier`).
4. `meta_research/__init__.py`: re-export `build_kg`, `write_kg`.
5. DESIGN.md: §2 file tree (+`kg.py`), §6.5 CLI (+`kg`), §6.4 runner step list,
   §7.3 (+`inspired_by`), new §7.6 schema.
6. `skills/meta-research/SKILL.md` + `REFERENCE.md` (and the copy under
   `examples/water_cooling/.claude/skills/meta-research/`): loop step 1 gains
   "Read kg.json to *locate* relevant prior bundles; the KG is a navigation
   index — every hypothesis must still cite actually-read bundles/heatmaps."
   Hypothesis hand-off docs gain `inspired_by`.

## 6. Out of scope (v1)

- CLI filter queries (`--param`, `--axis`, `--node`) — add when kg.json outgrows
  the context window.
- Incremental KG updates — revisit if bundle counts reach thousands.
- Visualization (graphviz/mermaid export).
- Deep/nested param diffing — nested values compare as opaque wholes.
