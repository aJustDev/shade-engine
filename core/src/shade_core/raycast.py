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

Deliberately kept: when the ray passes exactly through a cell corner --- the
diagonal sectors do, every step --- the two cells sharing that corner both come
out, one of them with zero thickness. Grazing a column's corner does block the
sun under the solid-column reading, and dropping it would be a different model,
not a tidier one.
"""

import math

__all__ = ["ray_cells"]


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
        if next_col < next_row:
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
