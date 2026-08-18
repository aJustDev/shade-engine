"""Analytical hillshading: turning a height raster into something with shape.

A flat mask of ``landcover == BUILDING`` tells you where a block is and stops
there: two neighbours sharing a party wall are one blob, because a mask has no
interior. What separates them is height -- the party wall, the setback, the
roof pitch -- and a hillshade is the cheapest way to put height on a map that
is otherwise about shade.

**What it computes.** Every cell of the surface has a normal; light it with one
distant lamp and the brightness is the cosine of the angle between the two:

    n = (-dz/dx, -dz/dy, 1) / |...|          x East, y North, z Up
    L = (sin(az) cos(alt), cos(az) cos(alt), sin(alt))
    shade = max(0, n . L)

``az`` is the direction the light comes *from* and follows the same convention
as everything else in this engine: **0 = North, clockwise**, the one
:mod:`shade_core.solar` uses for the sun. ``alt`` is its height over the
horizon. On flat ground the whole thing collapses to ``sin(alt)``, which is the
sanity check worth remembering.

**Why the light comes from the north-west by default, which the sun never
does.** Lit from the south-east -- where the sun actually is at midday in
Cordoba -- most people see the relief *inverted*: hills read as hollows. It is
a perceptual effect and it is strong, so cartography has lit from the top-left
since long before computers. This is a drawing, not a simulation; the module
that simulates the sun is :mod:`shade_core.solar` and it is not this one.

**Why on the DSM and not the DTM.** The DTM is bare ground: shading it would
draw the slope of the town, which here is nearly nothing. The DSM carries the
buildings, and the buildings are the subject.

**And why never after reprojecting.** A slope is a ratio of a height to a
*distance*, so it is only meaningful where the horizontal unit is a metre. In
Web Mercator the scale factor grows as ``1/cos(lat)`` -- about 1.26 at Cordoba's
latitude -- so the same roof would come out with a different pitch depending on
how far north it is. The rule this project applies to distances applies here
too: compute in the projected CRS of the city, then warp the *result*.

See ``shade-docs: learning/hillshade.md``.
"""

import math
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy import ndimage

DEFAULT_AZIMUTH_DEG: Final = 315.0
"""Light from the north-west: the cartographic convention, not the real sun."""

DEFAULT_ALTITUDE_DEG: Final = 45.0
"""Light 45 degrees over the horizon. Lower digs the contrast, higher flattens."""

DEFAULT_SMOOTH_SIGMA_PX: Final = 1.2
"""Gaussian blur applied to the surface before the gradient, in pixels.

A LiDAR DSM is rough at the scale of its own cell, and a gradient is exactly the
operator that turns roughness into noise. Measured over Montalban's building
pixels: the local standard deviation in a 3x3 window is **0.61 m median and
2.48 m at p90**, which over 1 m cells is tens of degrees of slope out of tiles,
chimneys, antennas and the sensor itself.

What it removes is **speckle and not contrast**, and those are two different
measurements. The histogram barely moves: over Montalban's roofs the shading
keeps a standard deviation of 0.285 either way, and the four tones stay spread
27/16/32/25 against 28/17/26/29. What collapses is the *spatial* mess --- cells
whose tone matches none of their four neighbours fall from **7.9% to 2.7%**, and
tone transitions from 290,879 to 187,059. So the roofscape survives and the salt
and pepper does not.

It costs nothing in truth because this raster is **a drawing**: the sweep reads
the DSM itself and never sees this. Party walls are metres of step and survive
any blur this small.
"""


def hillshade(
    elevation: npt.NDArray[np.floating],
    resolution_m: float,
    *,
    azimuth_deg: float = DEFAULT_AZIMUTH_DEG,
    altitude_deg: float = DEFAULT_ALTITUDE_DEG,
    vertical_exaggeration: float = 1.0,
    smooth_sigma_px: float = 0.0,
) -> npt.NDArray[np.float32]:
    """Illumination of ``elevation`` in [0, 1]; 0 is fully turned away.

    ``elevation`` is a north-up raster in the city's projected CRS, so **row
    index grows southward** and the north gradient is the negative of the row
    gradient. Getting that sign wrong lights the city from the south-east while
    claiming north-west, which looks plausible and is wrong.

    ``vertical_exaggeration`` multiplies the height before the gradient. At
    1.0 a 1 m step over a 1 m cell is 45 degrees, which is already plenty for
    a town; it exists because a *terrain* relief usually needs more.

    ``smooth_sigma_px`` blurs the surface before the gradient; zero here and
    :data:`DEFAULT_SMOOTH_SIGMA_PX` at the call site that draws a city, for the
    reason that constant explains.

    A cell with no elevation gets no illumination, explicitly: a central
    difference at that cell does not read the cell itself, so without the last
    line a hole would come out perfectly lit while its neighbours went NaN.
    Filling it with zero instead would draw flat ground where there is no data.
    """
    height = np.asarray(elevation, dtype=np.float32) * np.float32(vertical_exaggeration)
    surface = height
    if smooth_sigma_px > 0.0:
        surface = ndimage.gaussian_filter(height, sigma=smooth_sigma_px)
    dz_drow, dz_dcol = np.gradient(surface, np.float32(resolution_m))
    dz_dx = dz_dcol
    dz_dy = -dz_drow

    azimuth = math.radians(azimuth_deg)
    altitude = math.radians(altitude_deg)
    light_x = math.sin(azimuth) * math.cos(altitude)
    light_y = math.cos(azimuth) * math.cos(altitude)
    light_z = math.sin(altitude)

    norm = np.sqrt(dz_dx * dz_dx + dz_dy * dz_dy + np.float32(1.0))
    dot = (-dz_dx * light_x - dz_dy * light_y + light_z) / norm
    shaded: npt.NDArray[np.float32] = np.clip(dot, 0.0, 1.0).astype(np.float32)
    shaded[~np.isfinite(height)] = np.float32("nan")
    return shaded
