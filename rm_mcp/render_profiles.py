"""Render profiles: the two-mode config for how a page is rasterised.

A reMarkable page is rendered for one of two audiences, and they want opposite
things:

- **analysis** -- input for a vision model (Gemini/Claude). Strokes are thinned
  (width_scale 0.85) and drawn per-segment with pressure-scaled width to
  attenuate the rmscene stroke-merge connector artifact, on a clean white page
  the model reads best. This is what rm_pull_notebook / rm_pull_project bake in
  before the interpret pass.
- **publication** -- the drawing as artwork. Full stroke width, maximum
  anti-aliasing, NO analysis-only tweaks, cropped to the ink (the substrate
  renderer sizes the canvas to the device page, leaving large dead margins that
  are wrong for output), and never sent to vision.

Every profile setting maps to a `tools/rm_render_page.py` CLI flag via
`render_flags()`, passed straight through the runner -- including crop-to-ink
(`--crop`/`--crop-margin`), which was originally an MCP-side post-process step
and is now native to the renderer so the whole publication render is one pass.

Consumed via get_profile() by rm_render (roundtrip.render_doc), rm_pull_notebook
(server.py), and rm_pull_project (roundtrip.pull_project_doc) -- the single
source of truth for both the human-facing and model-input renders.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderProfile:
    """How to rasterise a page, end to end."""

    name: str

    # -- render stage (maps to rm_render_page.py flags) ----------------------
    supersample: int          # 1 | 2 | 3  (NxN then downsample; 3 = smoothest)
    width_scale: float        # multiplier on stroke widths (<1 thins, >1 thickens)
    pressure_width: bool      # per-segment pressure-scaled width (analysis only)
    template: bool            # draw the faint page template (dots/grid/lines)

    # -- crop-to-ink (now native: --crop / --crop-margin on the renderer) ----
    crop_to_ink: bool         # crop the PNG to the non-background bounding box
    crop_margin_px: int       # margin left around the ink when cropping
    background: str           # "white" | "transparent"

    # -- downstream ----------------------------------------------------------
    run_vision: bool          # feed the render to the interpret pass afterwards

    def render_flags(self) -> list[str]:
        """The rm_render_page.py flags this profile implies."""
        flags = [
            "--supersample", str(self.supersample),
            "--width-scale", str(self.width_scale),
        ]
        if self.pressure_width:
            flags.append("--pressure-width")
        if self.template:
            flags.append("--template")
        if self.background == "transparent":
            flags.append("--transparent")
        if self.crop_to_ink:
            flags += ["--crop", "--crop-margin", str(self.crop_margin_px)]
        return flags


# The analysis profile is the single source of truth for the model-input render
# (--supersample 2 --pressure-width --width-scale 0.85). rm_pull_notebook and
# rm_pull_project now resolve their interpret-pass flags from here via
# get_profile(), rather than hard-coding them; rm_render(profile="analysis")
# is the same render minus the interpret step, a faithful preview of what the
# vision model actually sees.
ANALYSIS = RenderProfile(
    name="analysis",
    supersample=2,
    width_scale=0.85,
    pressure_width=True,
    template=False,
    crop_to_ink=False,
    crop_margin_px=0,
    background="white",
    run_vision=True,
)

PUBLICATION = RenderProfile(
    name="publication",
    supersample=3,
    width_scale=1.0,
    pressure_width=False,
    template=False,
    crop_to_ink=True,
    crop_margin_px=60,
    background="white",   # default white; the rm_render tool's transparent= toggles this
    run_vision=False,
)

PROFILES: dict[str, RenderProfile] = {p.name: p for p in (ANALYSIS, PUBLICATION)}

DEFAULT_PROFILE = "publication"


def get_profile(name: str | None) -> RenderProfile:
    """Resolve a profile name (case-insensitive); default to publication."""
    key = (name or DEFAULT_PROFILE).strip().lower()
    if key not in PROFILES:
        raise ValueError(
            f"unknown render profile {name!r}: expected one of "
            f"{sorted(PROFILES)}"
        )
    return PROFILES[key]
