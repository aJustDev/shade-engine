"""Walking a ray across a grid of cells, exactly.

A DSM is not a cloud of points: it is a field of columns, one per cell, each
1 pixel square and as tall as its value. So "is the sun blocked from here" is
not a question about samples along a line -- it is a question about which
columns the line of sight actually enters, and how high it is when it gets
there.

That is what :func:`ray_cells` answers, with the grid traversal of Amanatides
& Woo (1987): from the observer's cell, walk to whichever cell boundary the
ray meets first, step into that neighbour, repeat. Every cell the ray touches
comes out exactly once, in order, with the distance at which the ray **enters**
it and the distance at which it **leaves**.

Those two distances are the physics, not bookkeeping:

- **Entry** is the most shade a column can justify. A ray that only climbs is
  at its lowest inside a cell right when it enters, so if it clears the column
  there, it clears it everywhere in the footprint.
- **Exit** is the least. A column would have to fill its whole cell -- and the
  ray stay below its top all the way through -- for this to be the answer.

A real obstacle sits somewhere between: a crown that lets light through, a
pierced parapet, a cell a wall only half occupies. Reporting both is how the
engine says what it knows and what it is assuming (see shade-docs:
learning/recorrido-de-rayo.md).

**Corner crossings.** When the ray passes exactly through a cell corner, four
cells meet there and the walk emits two: the diagonal one, plus *one* of the two
orthogonal neighbours, with zero thickness. This is not an edge case in the
sweep: the four diagonal sectors (8, 24, 40, 56 = NE, SE, SW, NW, so mid-morning
and mid-afternoon) cross a corner at *every* step, 354 of 354, while the other
sixty never do.

Emitting one is measured to be the best of the three readings, not merely the
tidiest. Against the arbiter over montilla-test and the 83 ladder instants, open
sky: emitting one gives 0.714% of wrong verdicts, both 0.722%, neither 0.739%.
The reason is that a sector is a 5.625-degree *interval* of azimuths, and
everywhere in it except the exact centre line the ray really does enter exactly
one of the two -- which one depending on the side. On the centre line the
function is genuinely discontinuous, and one is the honest average of its two
limits.

**Which one is a rule, not an accident.** The two boundary distances coincide
there, so whichever compares smaller is decided by an ulp of sin against cos
(1 to 3 ulp at 45 degrees, measured) -- and transcendentals are not required to
round identically across libms or architectures. Left alone, a libm update
would silently rewrite 3,675,077 cube cells (4.5%) with nobody touching the
code. So ties within :data:`CORNER_TOLERANCE` always step the **row** axis.

Row is arbitrary and permanent, and the arbitrariness is the point: the two
choices are equivalent by construction. Approach a diagonal from below and the
ray enters the row cell; approach from above and it enters the column cell; the
sector covers both halves equally. Measured, they do not separate: pinning row
gives 0.7305% and column 0.7059%, and today's accidental mix of two sectors each
lands at 0.7143%, which is their average -- what you see when the difference is
the city and not the rule. Row also moves the existing cube least (1.4 M cells
against 2.3 M).

The same tie decides the blocker class of the pair, since ascending distances
and a strict ``>`` give ties to whichever cell came out first. Measured on
montilla-test: flipping the order inside every tied pair moves 246 classes
across the four diagonal planes (0.005%) and **not one angle**, quantized or
float -- both cells are always visited, so only the tie-break can differ.

See shade-docs: learning/recorrido-de-rayo.md.
"""

import math

CORNER_TOLERANCE = 1e-9
"""Relative gap below which two cell boundaries are the same corner.

Wide enough to swallow the 1-3 ulp that separate sin from cos at 45 degrees,
and far too narrow to fire anywhere else: measured over all 64 sectors, the
only ties are the 354-per-sector of the four diagonals, and zero in the other
sixty.
"""

__all__ = ["CORNER_TOLERANCE", "ray_cells"]


def ray_cells(
    azimuth_deg: float, max_distance_m: float, resolution_m: float
) -> list[tuple[int, int, float, float]]:
    """Cells a ray crosses, as (d_row, d_col, entry_m, exit_m), ascending.

    Offsets are relative to the observer's cell, which is excluded: rows grow
    southward and columns eastward, so with azimuth 0 = North measured
    clockwise (the convention of :class:`shade_core.solar.SunPosition`) the ray
    direction is ``(sin, cos)`` in (east, north) and ``(-cos, sin)`` in
    (row, col).

    The walk stops at ``max_distance_m``; the last cell's exit is clamped to
    it. Distances are measured along the ray on the horizontal plane, which is
    what a horizon angle divides a height by.
    """
    azimuth = math.radians(azimuth_deg)
    d_col, d_row = math.sin(azimuth), -math.cos(azimuth)

    def schedule(direction: float) -> tuple[float, float, int]:
        """(distance to the first boundary, distance between boundaries, step).

        Infinite when the ray does not move along this axis, which keeps that
        boundary permanently out of reach instead of needing a special case in
        the loop below.
        """
        if direction == 0.0:
            return math.inf, math.inf, 0
        return (
            (0.5 * resolution_m) / abs(direction),
            resolution_m / abs(direction),
            (1 if direction > 0 else -1),
        )

    next_col, step_col_m, step_col = schedule(d_col)
    next_row, step_row_m, step_row = schedule(d_row)

    row = col = 0
    entries: list[tuple[int, int, float]] = []
    while True:
        # At an exact corner the two boundaries are the same distance, and which
        # one compares smaller is decided by an ulp of sin against cos -- a
        # property of libm, not of the geometry. Break the tie with a rule so a
        # rebuilt city cannot come out different somewhere else.
        corner = math.isclose(next_col, next_row, rel_tol=CORNER_TOLERANCE)
        if next_col < next_row and not corner:
            entry, next_col = next_col, next_col + step_col_m
            col += step_col
        else:
            entry, next_row = next_row, next_row + step_row_m
            row += step_row
        if entry >= max_distance_m:
            break
        entries.append((row, col, entry))

    # A cell is left exactly when the next one is entered; the last one is cut
    # off by the radius.
    return [
        (
            row,
            col,
            entry,
            entries[index + 1][2] if index + 1 < len(entries) else max_distance_m,
        )
        for index, (row, col, entry) in enumerate(entries)
    ]
