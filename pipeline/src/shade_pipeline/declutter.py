"""Take the clutter out of the DSM before anything casts a shadow from it.

Everything else in this pipeline treats the DSM as a record: whatever the
LiDAR saw, at the height it saw it. :mod:`shade_pipeline.footprints` says so
in as many words -- only the label moves, the heights are untouched. This
module is the deliberate exception, and it needs its own justification
(``shade-docs: decisions/ADR-022.md``).

The reason is that a surface model built from one aerial pass records two
different kinds of thing and cannot tell them apart. There is the permanent
fabric of the city -- ground, walls, roofs, trees -- which is what a shade map
is about. And there is whatever happened to be in the air on the morning of
the flight: cables strung across a square, market awnings, a lorry. The engine
answers "will there be shade here next Tuesday at five", so a cable is not
just noise, it is a wrong answer given confidently.

Two shapes are removed, both measured on the Plaza de la Corredera:

- **Thin linear features.** A grey opening wider than a cable erases it and
  leaves everything wider intact, so ``dsm - opening`` is the height by which
  each pixel sticks out of its own surroundings. A building's edge does not
  stick out at all -- the opening restores it -- while a cable sticks out its
  full height. Of what protrudes, only the regions one or two pixels wide,
  eight or more long, and hanging **over open ground** are removed. A 5 cm
  cable is not a 1 m wall: deleting it makes the model *more* faithful, not
  less.
- **Low flat slabs over open ground.** An awning is one to three metres up,
  as flat as a table (sd of the CHM 0.06-0.07 where a pruned orange tree
  measures 0.26-0.52), and stands surrounded by paving with nothing else near
  it, and the classifier did not call it a building. Given the observer stands
  at 1.6 m, these change very little shade; they are removed because they are
  transitory, and because leaving them in means the canopy mask has to keep
  arguing with them.

Every guard here was added after measuring the first run rather than reasoned
out in advance, and they all point the same way: **remove only what is
unambiguous**. A line is only clutter if it hangs over open ground and nobody
has drawn a building there; a slab is only clutter if the classifier did not
call it a building. Between an awning left standing and a shed deleted, the
shed is the worse mistake -- it is real, and it will still be there next
Tuesday.

What this buys, and what it costs: the height safety net is gone. Until now
any classification mistake was cosmetic, because the sweep read a DSM nobody
had edited. From here a bug in this module removes real obstacles, so the
build records how many pixels it touched of each kind, and a baseline built
before this existed is no longer the right thing to diff a new one against.
"""

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy import ndimage

from shade_core.shade import Landcover

LINEAR_OPENING_PX: Final = 5
"""Side of the grey opening that defines "its own surroundings", in pixels.

Wider than the clutter being hunted (1-2 px) and narrower than anything the
city is made of, so a roof, a wall and a crown all survive it whole -- opening
restores every structure wider than the element, edges included.
"""

LINEAR_PROTRUSION_M: Final = 3.0
"""How far a pixel must stick out of its surroundings before it is suspect.

Three metres is above the reach of surface roughness and below the smallest
thing anybody would call a structure, so what lands here is what hangs in the
air.
"""

LINEAR_MAX_BASE_M: Final = 2.0
"""How high over the terrain the thing a line hangs above may be.

The rule that turns a plausible filter into a defensible one. A cable over a
square and a party wall standing proud of a roof look identical from above:
both are two pixels wide, both stick out of their surroundings, and nothing in
a surface model says which one has air underneath. What does separate them is
what they hang over.

Measured on ``cordoba-test``: of 407 linear regions, **389 hang over open
ground** and are the market cables and lamppost rigging this module is for.
The other 18 are large rooftop structures, and they are left alone -- their
shadow falls on a roof, where nobody is standing.
"""

LINEAR_WIDTH_PX: Final = 2
"""Pixels of area per unit of length below which a region is a line, not a blob."""

LINEAR_MIN_PX: Final = 8
"""Length, in pixels, above which a thin region is clutter and not a corner."""

SLAB_MIN_HEIGHT_M: Final = 1.0
SLAB_MAX_HEIGHT_M: Final = 3.0
"""CHM band a slab lives in: over head height, under a first floor."""

SLAB_ROUGHNESS_MAX_M: Final = 0.20
"""Flatness a slab must not exceed, the same threshold the canopy mask uses.

Set by the orange trees rather than by the awnings: see
:data:`shade_pipeline.canopy.CANOPY_ROUGHNESS_MIN_M`.
"""

SLAB_GROUND_FRACTION: Final = 0.75
"""Share of a slab's outline that must border open ground for it to be one.

An awning in the middle of a square is ringed by paving. A flat roof terrace
of the same height and flatness is ringed by its own building, and stays.
"""

STRUCTURE_8: Final = np.ones((3, 3), dtype=np.uint8)
"""8-connectivity: a diagonal step is a step, for cables and crowns alike."""


@dataclass(frozen=True)
class DeclutterResult:
    """How much of the DSM was rewritten, by reason."""

    linear_px: int
    slab_px: int

    @property
    def total_px(self) -> int:
        return self.linear_px + self.slab_px


def linear_labels(
    labels: npt.NDArray[np.integer],
    count: int,
    *,
    width_px: int = LINEAR_WIDTH_PX,
    min_length_px: int = LINEAR_MIN_PX,
) -> npt.NDArray[np.bool_]:
    """Which labelled regions are lines: one per label, index 0 the background.

    Thinness is measured as area against the longest side of the bounding box,
    not as the shorter side of that box. A cable running diagonally has a box
    as tall as it is wide, and the box-side test would call it compact; the
    area test sees that it fills two pixels per step and calls it a line.
    """
    flags = np.zeros(count + 1, dtype=bool)
    if count == 0:
        return flags
    areas = np.bincount(labels.ravel(), minlength=count + 1)[1:]
    # find_objects returns None for a label absent from the array, which
    # ndimage.label never produces: its labels run 1..count with no gaps.
    longest = np.array(
        [
            0 if box is None else max(box[0].stop - box[0].start, box[1].stop - box[1].start)
            for box in ndimage.find_objects(labels)
        ],
        dtype=np.int64,
    )
    flags[1:] = (longest >= min_length_px) & (areas <= width_px * longest)
    return flags


def _remove_linear(
    dsm: npt.NDArray[np.float32],
    dtm: npt.NDArray[np.float32],
    built: npt.NDArray[np.bool_] | None,
    *,
    opening_px: int,
    protrusion_m: float,
    max_base_m: float,
) -> int:
    """Flatten thin, long protrusions back onto their surroundings, in place.

    ``built`` marks the pixels somebody has drawn a building over, and they
    are never touched. It is the guard with the most reach, because the
    linear rule is the one that removes height: a narrow tall structure --
    an exposed party wall, a bell tower, a building one room deep -- is thin,
    long, and stands over its own street exactly like a cable does. Measured
    on ``cordoba-test``: of 232 linear regions, 73 touch an OSM footprint;
    dropping those takes the removal from 4,327 px to 2,982 and leaves only
    six regions taking more than 20 m off anything.
    """
    surroundings = ndimage.grey_opening(dsm, size=(opening_px, opening_px))
    protruding = ((dsm - surroundings) >= protrusion_m) & ((surroundings - dtm) < max_base_m)
    if not protruding.any():
        return 0
    labels, count = ndimage.label(protruding, structure=STRUCTURE_8)
    drop = linear_labels(labels, count)
    if built is not None:
        # Whole regions, after labelling and never before it. Masking the
        # pixels first would let the guard *create* victims: a compact region
        # with a footprint over half of it comes out of the mask as a thin
        # sliver, and the shape test then calls that sliver a cable.
        touches = np.bincount(labels[built], minlength=count + 1) > 0
        touches[0] = False
        drop &= ~touches
    if not drop.any():
        return 0
    clutter = drop[labels]
    # Never below the terrain. A grey opening is a local minimum in disguise,
    # and the DTM is interpolated under buildings, so around a courtyard the
    # restored surroundings can land under the ground the DTM claims is there.
    # Left alone that breaks ``dsm >= dtm``, which verify checks and the sweep
    # assumes; measured on cordoba-test, 445 px of a 9 Mpx grid.
    dsm[clutter] = np.maximum(surroundings[clutter], dtm[clutter])
    return int(clutter.sum())


def _remove_slabs(
    dsm: npt.NDArray[np.float32],
    dtm: npt.NDArray[np.float32],
    landcover: npt.NDArray[np.uint8],
    *,
    min_height_m: float,
    max_height_m: float,
    roughness_max_m: float,
    ground_fraction: float,
) -> int:
    """Drop low flat slabs standing over open ground back to the terrain, in place.

    Anything the classifier called a building is off limits, and the asymmetry
    is deliberate. This whole module exists because the PNOA's *vegetation*
    label is unreliable; its *building* label is the side it gets right, since
    a flat roof is exactly what a geometric classifier recognises. So the
    label is trusted to protect and never to accuse. Measured on
    ``cordoba-test``: it spares 830 px of the 30,815 the geometry alone would
    have taken -- single-storey sheds and porches standing in open plots,
    which are permanent fabric and not awnings.
    """
    chm = dsm - dtm
    candidates = (chm >= min_height_m) & (chm <= max_height_m) & (landcover != Landcover.BUILDING)
    if not candidates.any():
        return 0
    labels, count = ndimage.label(candidates, structure=STRUCTURE_8)
    if count == 0:
        return 0
    index = np.arange(1, count + 1)
    flat = (
        np.asarray(ndimage.standard_deviation(chm, labels=labels, index=index), dtype=np.float64)
        < roughness_max_m
    )

    # The outline of every region at once: dilating the label image hands each
    # background pixel the label of the region it touches, so one bincount per
    # side gives the ground fraction of every outline without a Python loop.
    outline = ndimage.grey_dilation(labels, size=(3, 3))
    on_outline = (labels == 0) & (outline > 0)
    outline_labels = outline[on_outline]
    open_ground = (chm < min_height_m)[on_outline]
    total = np.bincount(outline_labels, minlength=count + 1)[1:]
    on_ground = np.bincount(outline_labels, weights=open_ground, minlength=count + 1)[1:]
    with np.errstate(invalid="ignore"):
        isolated = np.divide(on_ground, total, out=np.zeros_like(on_ground), where=total > 0)

    drop = np.concatenate(([False], flat & (isolated >= ground_fraction)))
    if not drop.any():
        return 0
    slabs = drop[labels]
    dsm[slabs] = dtm[slabs]
    return int(slabs.sum())


def declutter_dsm(
    dsm: npt.NDArray[np.float32],
    dtm: npt.NDArray[np.float32],
    landcover: npt.NDArray[np.uint8],
    built: npt.NDArray[np.bool_] | None = None,
    *,
    opening_px: int = LINEAR_OPENING_PX,
    protrusion_m: float = LINEAR_PROTRUSION_M,
    max_base_m: float = LINEAR_MAX_BASE_M,
    slab_min_height_m: float = SLAB_MIN_HEIGHT_M,
    slab_max_height_m: float = SLAB_MAX_HEIGHT_M,
    slab_roughness_max_m: float = SLAB_ROUGHNESS_MAX_M,
    slab_ground_fraction: float = SLAB_GROUND_FRACTION,
) -> DeclutterResult:
    """Rewrite ``dsm`` in place, removing cables and awnings; report the count.

    In place because the array is the padded city grid and a copy of it is
    gigabytes. Linear features go first: an awning hanging off a cable stops
    being connected to it once the cable is gone, and is then judged on its own
    flatness.

    ``built`` is the OSM footprint mask (see :mod:`shade_pipeline.footprints`),
    or None for a build with no network, which simply loses that guard.
    """
    linear_px = _remove_linear(
        dsm, dtm, built, opening_px=opening_px, protrusion_m=protrusion_m, max_base_m=max_base_m
    )
    slab_px = _remove_slabs(
        dsm,
        dtm,
        landcover,
        min_height_m=slab_min_height_m,
        max_height_m=slab_max_height_m,
        roughness_max_m=slab_roughness_max_m,
        ground_fraction=slab_ground_fraction,
    )
    return DeclutterResult(linear_px=linear_px, slab_px=slab_px)
