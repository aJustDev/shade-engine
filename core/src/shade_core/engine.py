"""Which engine computed a city's rasters, as a number the tooling can compare.

An artifact directory already says two versions about itself and neither one
moves when the *numbers inside the rasters* do: ``artifact_version`` ("v1")
names the layout and is a path segment, and ``schema_version`` says which fields
``metadata.json`` carries. Phase 13 changed the values four times in two days --
float32 over relative heights ([[ADR-026]]), the ray traversal ([[ADR-027]]),
the radius bounded by geometry ([[ADR-028]]) and the corner tie-break -- and
every city on disk went on reporting itself as done, because nothing recorded
which engine had produced it.

The package versions cannot answer it. ``metadata.json`` records
``shade-pipeline: 0.1.0`` and ``shade-core: 0.1.0``, which is what
``pyproject.toml`` said before the phase started and what it says now. Nor can a
git hash: it moves on every commit, including the ones that change no output at
all, and a build from a dirty tree has no honest one. So it is an integer,
bumped by hand, and the docstring below is the whole of its meaning. See
``shade-docs: decisions/ADR-029-el-artefacto-declara-su-motor.md``.

A module of its own, importing nothing, for the reason that already moved
``CHAIN`` out of ``runner``: ``shade_pipeline.runstate`` costs 121 ms to import
and is what the console draws its first table from, while
``shade_core.artifacts`` costs 835 ms because it brings rasterio. Staleness is
derived on every console refresh, so what it compares against has to be free to
reach.
"""

from typing import Final

ARTIFACT_ENGINE_VERSION: Final = 1
"""Which generation of engine computed the cubes in an artifact directory.

1: the ray is traversed cell by cell and a cell blocks from its near edge
([[ADR-027]]); the sweep computes in float32 over heights relative to a datum
([[ADR-026]]); the radius is the only bound, with the ``geometric`` schedule
withdrawn ([[ADR-028]]); and a corner tie is broken by row.

Absent (``None``) means older than all of that, which is the only thing that can
be said honestly about a file written before the field existed -- which is why
this starts at 1 rather than numbering the past. Bump it when a change moves the
values in the cubes, and add a line here saying what moved: the number is worth
exactly what this list says about it.
"""
