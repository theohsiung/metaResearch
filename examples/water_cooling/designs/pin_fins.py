"""Baseline candidate: an inline copper pin-fin cold plate.

The second seed design (DESIGN.md §4 BASELINES). Instead of continuous parallel
fins, the wetted surface is broken into a regular grid of short cylindrical pins
in an *inline* arrangement (rows aligned with the flow). Pin fins repeatedly trip
and restart the thermal boundary layer, raising the heat-transfer coefficient
relative to straight fins -- at the cost of higher pressure drop. It typically
anchors the lower-thermal-resistance end of the Pareto frontier opposite
``straight_fins``.

Mechanism axis: geometry + arrangement (discretized pins, inline) -- see the
meta-research skill's mechanism-axis catalog. A natural one-mechanism follow-up is
switching ``arrangement`` to "staggered".

Envelope: OPERATING["envelope_mm"] = {L:40, W:40, max_height:20}.
Feasibility (DESIGN.md §9) -- all satisfied by construction:
  * fin_thickness_mm = 1.0 (pin diameter)  >= 0.3
  * aspect ratio = 8.0 / 1.0 = 8.0          <= 30
  * footprint 40 x 40                        fits the 40 x 40 envelope
  * stack height = t_base + fin = 3 + 8 = 11 mm <= max_height (20)
  * pitch = 40 / 10 = 4.0 mm                 >= fin_thickness + 0.3 = 1.3
                                             (both row and column pitch)

Module contract (DESIGN.md §5): exposes ``build() -> DesignSpec`` and ``NAME``.
"""

from __future__ import annotations

from meta_research.interfaces import DesignSpec

NAME = "pin_fins"


def build() -> DesignSpec:
    """Construct the inline pin-fin baseline cold-plate design.

    Returns:
        A :class:`DesignSpec` whose ``params`` describe a copper inline pin-fin
        plate sized to the 40 x 40 x 20 mm envelope (10 x 10 grid of 1.0 mm pins,
        8 mm tall, 4.0 mm pitch). Purely parametric -- no artifacts.
    """
    params: dict[str, object] = {
        "fin_type": "pin",
        "L_mm": 40.0,          # flow-direction length of the base plate
        "W_mm": 40.0,          # span across the pin grid
        "t_base_mm": 3.0,      # solid base thickness under the pins
        "fin_height_mm": 8.0,
        "fin_thickness_mm": 1.0,   # pin diameter
        "n_rows": 10,          # rows along the flow direction (L)
        "n_cols": 10,          # columns across the span (W)
        "pitch_mm": 4.0,       # uniform center-to-center spacing (40 / 10)
        "arrangement": "inline",
        "material": "copper",  # k = 400 W/mK
    }
    return DesignSpec(
        params=params,
        artifacts={},
        notes=(
            "Baseline copper inline pin-fin cold plate: 10 x 10 grid of 1.0 mm "
            "diameter pins, 8 mm tall, on a 3 mm base, 4.0 mm pitch. Boundary-layer "
            "restart raises h vs. straight fins; anchors the low-R_th / higher-dP "
            "end of the frontier. Natural follow-up: arrangement -> 'staggered'."
        ),
    )
