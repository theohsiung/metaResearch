"""Baseline candidate: a copper straight-fin cold plate.

This is one of the two seed designs (DESIGN.md §4 BASELINES). It encodes the most
conventional cold-plate geometry: a row of thin, parallel rectangular fins
machined into a copper base, with water flowing along the channels between them.
It exists to anchor the Pareto frontier with a familiar, manufacturable point --
typically low pressure drop, moderate thermal resistance.

Mechanism axis: geometry (parallel straight fins) -- see the meta-research skill's
mechanism-axis catalog. The agent's later candidates depart from this baseline by
changing exactly one mechanism (density, distribution, arrangement, material, ...).

Envelope: OPERATING["envelope_mm"] = {L:40, W:40, max_height:20}.
Feasibility (DESIGN.md §9) -- all satisfied by construction:
  * fin_thickness_mm = 0.8           >= 0.3
  * aspect ratio = 10.0 / 0.8 = 12.5 <= 30
  * footprint 40 x 40                fits the 40 x 40 envelope
  * stack height = t_base + fin = 3 + 10 = 13 mm <= max_height (20)
  * pitch = 40 / 20 = 2.0 mm         >= fin_thickness + 0.3 = 1.1

Module contract (DESIGN.md §5): exposes a module-level ``build() -> DesignSpec``
and an optional ``NAME``. ``build()`` is pure and succeeds from a cold start.
"""

from __future__ import annotations

from meta_research.interfaces import DesignSpec

NAME = "straight_fins"


def build() -> DesignSpec:
    """Construct the straight-fin baseline cold-plate design.

    Returns:
        A :class:`DesignSpec` whose ``params`` fully describe a copper straight-fin
        plate sized to the 40 x 40 x 20 mm envelope. No artifacts (purely
        parametric); the evaluator derives all geometry from these params.
    """
    params: dict[str, object] = {
        "fin_type": "straight",
        "L_mm": 40.0,          # flow-direction length of the base plate
        "W_mm": 40.0,          # span across which fins are distributed
        "t_base_mm": 3.0,      # solid base thickness under the fins
        "fin_height_mm": 10.0,
        "fin_thickness_mm": 0.8,
        "n_fins": 20,          # 20 fins across W=40 -> 2.0 mm pitch
        "material": "copper",  # k = 400 W/mK
    }
    return DesignSpec(
        params=params,
        artifacts={},
        notes=(
            "Baseline copper straight-fin cold plate: 20 parallel rectangular fins, "
            "10 mm tall, 0.8 mm thick, on a 3 mm base, 2.0 mm pitch. Conventional, "
            "manufacturable reference point anchoring the low-dP end of the frontier."
        ),
    )
