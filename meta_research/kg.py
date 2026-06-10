"""Derived knowledge graph over the experience ledger (DESIGN §7.6).

The KG is a **navigation index**, not a second store: it is rebuilt
deterministically from the experience bundles (the single source of truth) and
contains *facts only* — candidate nodes, parameter diffs, score deltas, and
lineage pointers. Interpretation (reasoning prose, expectations) stays in each
bundle's ``hypothesis.md``, which the agent reads by following the node's
``bundle`` pointer. Per the Meta-Harness discipline, the KG must never replace
reading the raw experience; it only answers "where should I look?".

Like :mod:`meta_research.frontier`, every rebuild recomputes the full graph
from the ledger — derived views are recomputed, only bundles are appended.
"""

from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------------------
# Fact extraction helpers
# ----------------------------------------------------------------------------


def param_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Shallow top-level diff between two ``DesignSpec.params`` dicts.

    Returns ``{"changed": {key: [old, new]}, "added": {key: new},
    "removed": {key: old}}``. Values are kept verbatim; non-scalar values
    (lists, dicts) are compared as opaque wholes — no deep diffing.
    """
    changed: dict[str, Any] = {}
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    for key, new_value in new.items():
        if key not in old:
            added[key] = new_value
        elif old[key] != new_value:
            changed[key] = [old[key], new_value]
    for key, old_value in old.items():
        if key not in new:
            removed[key] = old_value
    return {"changed": changed, "added": added, "removed": removed}


__all__ = ["param_diff"]
