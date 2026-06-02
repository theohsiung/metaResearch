"""Self-contained numpy cold-plate fin thermal + hydraulic surrogate.

``WaterCoolingEvaluator`` scores a liquid-cooled cold-plate fin design against two
objectives (thermal resistance and pressure drop) using a **trend-correct
surrogate**: standard textbook correlations (hydraulic diameter, Dittus-Boelter /
laminar Nusselt, fin efficiency, friction-factor pressure drop) wired together in
pure numpy. It is *not* a CFD solver -- absolute numbers are approximate, but the
sensitivities (more/denser/taller fins -> lower R_th but higher dP; copper vs
aluminum; laminar vs turbulent transition) move the right way, which is what the
research loop optimizes against.

Implements the ``Evaluator`` protocol from :mod:`meta_research.interfaces`
directly: it declares ``objectives`` and an ``evaluate`` method that NEVER raises
(every failure is wrapped in ``EvalResult.crashed(...)``). The constructor takes
the OPERATING dict (heat load, flow, inlet temperature, envelope).

Artifacts written to ``out_dir``:
  * ``heatmap.png`` -- diffused base-plate temperature field (the diagnostic the
    agent reads back next iteration).
  * ``fields.npz`` -- raw temperature grid + velocity scalar.
  * ``breakdown.json`` -- per-mechanism resistance / pressure breakdown (becomes
    ``EvalResult.metadata``).

Per DESIGN.md s9. Kept deliberately small (< ~300 lines).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless: no display, render to file only.
import matplotlib.pyplot as plt  # noqa: E402  (must follow use("Agg"))
import numpy as np  # noqa: E402

from meta_research.interfaces import DesignSpec, EvalResult, Objective  # noqa: E402

# --- Physical constants -----------------------------------------------------
# Water properties at ~40 C (the typical mean coolant temperature here).
RHO = 992.0        # density            [kg/m^3]
CP = 4178.0        # specific heat       [J/kg-K]
MU = 6.5e-4        # dynamic viscosity   [Pa-s]
K_F = 0.63         # fluid conductivity  [W/m-K]
PR = 4.3           # Prandtl number      [-]

K_MATERIAL = {"copper": 400.0, "aluminum": 200.0}  # solid k [W/m-K]
DENSITY_MATERIAL = {"copper": 8960.0, "aluminum": 2700.0}  # [kg/m^3]

RE_LAMINAR = 2300.0  # laminar -> turbulent transition Reynolds number.
NU_LAMINAR = 3.66    # fully-developed laminar Nu, constant wall temperature.
K_MINOR = 1.5        # combined entrance/exit minor-loss coefficient.

# Manufacturing / feasibility limits (DESIGN s9.7).
MIN_FIN_THICKNESS_MM = 0.3
MAX_ASPECT_RATIO = 30.0
MIN_PITCH_GAP_MM = 0.3  # pitch must be >= t_fin + this gap.


class WaterCoolingEvaluator:
    """Scores a cold-plate fin design. Implements the Evaluator protocol directly."""

    def __init__(self, operating: dict[str, Any]) -> None:
        self.operating = dict(operating)
        self.objectives: list[Objective] = [
            Objective("thermal_resistance", "min", "K/W", 1.0),
            Objective("pressure_drop", "min", "Pa", 0.3),
        ]

    # -- public protocol method ---------------------------------------------
    def evaluate(self, design: DesignSpec, out_dir: Path) -> EvalResult:
        """Score one design. Never raises; failures become EvalResult.crashed()."""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            scores, metadata, feasible, grid = self._simulate(design.params)
            arts = self._write_artifacts(out_dir, scores, metadata, grid)
            return EvalResult(
                scores=scores, feasible=feasible, metadata=metadata, artifacts=arts
            )
        except Exception as exc:  # evaluators MUST never raise
            return EvalResult.crashed(f"{type(exc).__name__}: {exc}")

    # -- physics -------------------------------------------------------------
    def _simulate(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, float], dict[str, Any], bool, np.ndarray]:
        op = self.operating
        heat_w = float(op["heat_load_W"])
        flow_lpm = float(op["flow_lpm"])
        env = op["envelope_mm"]
        max_h_mm = float(env["max_height"])

        fin_type = str(params.get("fin_type", "straight"))
        material = str(params.get("material", "copper"))
        k_s = K_MATERIAL.get(material, K_MATERIAL["copper"])
        rho_s = DENSITY_MATERIAL.get(material, DENSITY_MATERIAL["copper"])

        # Geometry in metres.
        L = float(params["L_mm"]) / 1000.0
        W = float(params["W_mm"]) / 1000.0
        t_base = float(params["t_base_mm"]) / 1000.0
        h_fin = float(params["fin_height_mm"]) / 1000.0
        t_fin = float(params["fin_thickness_mm"]) / 1000.0

        Q_flow = flow_lpm / 1000.0 / 60.0  # litre/min -> m^3/s
        A_plate = L * W

        # --- passage geometry, free-flow area, fin area ---------------------
        if fin_type == "pin":
            n_rows, n_cols, pitch = self._pin_layout(params, L, W)
            n_fins = n_rows * n_cols
            # Free-flow area = inlet cross-section minus pin blockage in one column.
            blockage = n_cols * t_fin
            free_w = max(W - blockage, 1e-6)
            A_free = free_w * h_fin
            # Wetted/heat-transfer area: pin side walls (square pins) + exposed base.
            A_fin = n_fins * (4.0 * t_fin * h_fin)
            A_base_unfinned = max(A_plate - n_fins * (t_fin * t_fin), 0.0)
            D_h = t_fin  # characteristic pin width ~ hydraulic scale
            L_flow = L
            P_fin = 4.0 * t_fin           # pin perimeter
            Ac_fin = t_fin * t_fin        # pin cross-section
            Lc = h_fin + t_fin / 4.0      # corrected pin length (tip area lumped)
            solid_vol = n_fins * Ac_fin * h_fin + A_plate * t_base
        else:  # straight fins (default)
            n_fins, pitch = self._straight_layout(params, W, t_fin)
            n_channels = max(n_fins - 1, 1)
            channel_w = max(pitch - t_fin, 1e-6)
            A_free = n_channels * channel_w * h_fin
            A_fin = n_fins * (2.0 * h_fin * L)  # both sides of each fin
            A_base_unfinned = max(A_plate - n_fins * (t_fin * L), 0.0)
            D_h = 2.0 * channel_w * h_fin / (channel_w + h_fin)  # rectangular channel
            L_flow = L
            P_fin = 2.0 * (L + t_fin)     # straight-fin perimeter
            Ac_fin = t_fin * L            # straight-fin cross-section
            Lc = h_fin + t_fin / 2.0      # corrected fin length
            solid_vol = n_fins * t_fin * h_fin * L + A_plate * t_base

        # --- velocity, Reynolds --------------------------------------------
        V = Q_flow / max(A_free, 1e-9)
        Re = RHO * V * D_h / MU

        # --- Nusselt, convection coefficient -------------------------------
        if Re < RE_LAMINAR:
            # Developing-flow Hausen-style bump over fully-developed Nu_laminar.
            gz = D_h / max(L_flow, 1e-9) * Re * PR
            Nu = NU_LAMINAR + 0.0668 * gz / (1.0 + 0.04 * gz ** (2.0 / 3.0))
        else:
            Nu = 0.023 * Re ** 0.8 * PR ** 0.4  # Dittus-Boelter (heating)
        h = Nu * K_F / max(D_h, 1e-9)

        # --- fin efficiency -------------------------------------------------
        m = math.sqrt(2.0 * h * P_fin / max(k_s * Ac_fin, 1e-12)) if h > 0 else 0.0
        mLc = m * Lc
        eta = (math.tanh(mLc) / mLc) if mLc > 1e-6 else 1.0
        A_eff = A_base_unfinned + eta * A_fin

        # --- resistances ----------------------------------------------------
        R_conv = 1.0 / max(h * A_eff, 1e-12)
        R_caloric = 1.0 / max(RHO * Q_flow * CP, 1e-12)
        R_cond_base = t_base / max(k_s * A_plate, 1e-12)
        R_th = R_cond_base + R_conv + R_caloric
        base_temp_rise = heat_w * R_th

        # --- pressure drop --------------------------------------------------
        f = (64.0 / Re) if Re < RE_LAMINAR else (0.079 * Re ** -0.25)
        dyn = 0.5 * RHO * V * V
        dP_friction = f * (L_flow / max(D_h, 1e-9)) * dyn
        dP_minor = K_MINOR * dyn
        dP = dP_friction + dP_minor

        mass_g = solid_vol * rho_s * 1000.0

        # --- feasibility constraints (DESIGN s9.7) -------------------------
        t_fin_mm = float(params["fin_thickness_mm"])
        h_fin_mm = float(params["fin_height_mm"])
        aspect = h_fin_mm / max(t_fin_mm, 1e-9)
        fits_env = (
            float(params["L_mm"]) <= float(env["L"]) + 1e-6
            and float(params["W_mm"]) <= float(env["W"]) + 1e-6
        )
        feasible = bool(
            t_fin_mm >= MIN_FIN_THICKNESS_MM
            and aspect <= MAX_ASPECT_RATIO
            and fits_env
            and h_fin_mm <= max_h_mm + 1e-6
            and pitch * 1000.0 >= t_fin_mm + MIN_PITCH_GAP_MM
        )

        scores = {
            "thermal_resistance": float(R_th),
            "pressure_drop": float(dP),
        }
        metadata: dict[str, Any] = {
            "R_cond_base": float(R_cond_base),
            "R_conv": float(R_conv),
            "R_caloric": float(R_caloric),
            "R_th": float(R_th),
            "dP_friction": float(dP_friction),
            "dP_minor": float(dP_minor),
            "fin_efficiency": float(eta),
            "h": float(h),
            "Re": float(Re),
            "Nu": float(Nu),
            "V": float(V),
            "D_h": float(D_h),
            "A_eff": float(A_eff),
            "mass_g": float(mass_g),
            "base_temp_rise_C": float(base_temp_rise),
            "fin_type": fin_type,
            "material": material,
            "n_fins": int(n_fins),
        }
        grid = self._temperature_field(L, W, R_th, heat_w, op)
        return scores, metadata, feasible, grid

    @staticmethod
    def _straight_layout(
        params: dict[str, Any], W: float, t_fin: float
    ) -> tuple[int, float]:
        """Return (n_fins, pitch_m) for straight fins from n_fins or pitch_mm."""
        if params.get("n_fins") is not None:
            n_fins = max(int(params["n_fins"]), 2)
            pitch = W / n_fins
        else:
            pitch = float(params.get("pitch_mm", 2.0)) / 1000.0
            n_fins = max(int(W / max(pitch, 1e-6)), 2)
            pitch = W / n_fins
        return n_fins, pitch

    @staticmethod
    def _pin_layout(
        params: dict[str, Any], L: float, W: float
    ) -> tuple[int, int, float]:
        """Return (n_rows, n_cols, pitch_m) for pin fins."""
        if params.get("n_rows") is not None and params.get("n_cols") is not None:
            n_rows = max(int(params["n_rows"]), 1)
            n_cols = max(int(params["n_cols"]), 1)
            pitch = min(L / n_rows, W / n_cols)
        else:
            pitch = float(params.get("pitch_mm", 2.0)) / 1000.0
            n_rows = max(int(L / max(pitch, 1e-6)), 1)
            n_cols = max(int(W / max(pitch, 1e-6)), 1)
        return n_rows, n_cols, pitch

    # -- diagnostic temperature field ---------------------------------------
    @staticmethod
    def _temperature_field(
        L: float, W: float, R_th: float, heat_w: float, op: dict[str, Any]
    ) -> np.ndarray:
        """Coarse base-plate temperature field: Gaussian hot spot diffused with a
        sink proportional to 1/R_th (better cooling => more uniform field)."""
        ny, nx = 48, max(int(48 * (L / max(W, 1e-9))), 24)
        inlet = float(op.get("inlet_temp_C", 25.0))
        ys, xs = np.mgrid[0:ny, 0:nx]
        cy, cx = ny * 0.5, nx * 0.45  # heat source slightly toward inlet half
        sigma = min(nx, ny) * 0.18
        peak = heat_w * R_th  # base temperature rise scales the hot spot
        field = inlet + peak * np.exp(
            -(((xs - cx) ** 2 + (ys - cy) ** 2) / (2.0 * sigma ** 2))
        )
        sink = 0.25 / (1.0 + R_th)  # stronger sink for lower R_th
        for _ in range(6):  # a few Jacobi smoothing iterations
            lap = (
                np.roll(field, 1, 0)
                + np.roll(field, -1, 0)
                + np.roll(field, 1, 1)
                + np.roll(field, -1, 1)
                - 4.0 * field
            )
            field = field + 0.2 * lap - sink * (field - inlet)
        return np.asarray(field, dtype=float)

    # -- artifacts -----------------------------------------------------------
    def _write_artifacts(
        self,
        out_dir: Path,
        scores: dict[str, float],
        metadata: dict[str, Any],
        grid: np.ndarray,
    ) -> dict[str, str]:
        heatmap = out_dir / "heatmap.png"
        fields = out_dir / "fields.npz"
        breakdown = out_dir / "breakdown.json"

        fig, ax = plt.subplots(figsize=(6.0, 4.5))
        im = ax.imshow(grid, origin="lower", cmap="inferno", aspect="auto")
        fig.colorbar(im, ax=ax, label="Temperature [C]")
        dT = metadata.get("base_temp_rise_C", 0.0)
        ax.set_title(
            f"Cold-plate base temp  |  R_th={scores['thermal_resistance']:.4f} K/W"
            f"  ΔT={dT:.1f} C"
        )
        ax.set_xlabel("flow direction (L)")
        ax.set_ylabel("width (W)")
        fig.tight_layout()
        fig.savefig(heatmap, dpi=90)
        plt.close(fig)

        np.savez_compressed(
            fields, temperature=grid, velocity=float(metadata.get("V", 0.0))
        )
        breakdown.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        return {
            "heatmap": "heatmap.png",
            "fields": "fields.npz",
            "breakdown": "breakdown.json",
        }
