# The bench

The scripts behind the numbers in the ADRs: the sweeps of Montilla that decided
float32 (S2), the distance convention (S3) and the radius (S4). They are not
tests. `tests/` pins what the code must do; the bench measures what it costs on
real data, and its output is a figure somebody then has to defend in writing.

**Why they are in git.** They used to live under `data/`, which is ignored. The
directory of an earlier round, `data/analisis-horizonte/`, no longer exists on
any machine, and two documents still cite it as the source of their numbers. A
figure whose script is gone is a figure nobody can question.

**What is still not in git.** Everything these scripts read and write: the
LiDAR, the raster stacks, the reference cubes and the JSON results, all under
`data/bench/`. That is 2.2 GB against 252 KB of code, and almost all of it is
regenerable from the commands below -- the exceptions are named further down,
and they are the interesting part.

## Running

From the repo root, always -- every path inside is relative to it:

    uv run python bench/bench_s4_edges.py

`ruff` skips this directory on purpose (`extend-exclude` in the root
`pyproject.toml`). Each script is the exact thing that produced a figure some
ADR now defends, and reformatting it after the fact breaks that correspondence
for no gain.

## Regenerating the inputs

| Input | Command |
| --- | --- |
| `data/lidar/montilla/*.laz` and `data/cities/montilla-test/v1/` | `uv run shade-engine build montilla-test --lidar-dir data/lidar/montilla` |
| `data/bench/montilla-test-padded.npz`, the padded stack the sweep really saw | `uv run python bench/prep_stack.py data/bench/montilla-test-padded.npz` |
| `data/bench/montilla-test-wide.npz`, the same padded to 950 m for the far field | `uv run python bench/prep_stack_wide.py` |
| `data/bench/montilla-test-s3/v1`, the reference cube every S4 comparison uses | `uv run python bench/rebuild_s3_reference.py` |
| `data/bench/cordoba-mdt25.tif` | the `curl` written in `bench_s4_cordoba_sierra.py` |

## Reference cubes, and what still runs

### The reference cubes are the fragile part

`data/bench/montilla-test-s3/v1` is what every S4 measurement compares against,
and on 2026-08-18 its `horizon.tif` was overwritten with a copy of
`montilla-test-tie-row`. Nothing in the code had changed, and the exit gate
still went red -- twice, with two different numbers. What rescued it was that
the other two cubes of the same directory survived: rebuilding the S3 sweep with
the old tie-break reproduced `blocker_class.tif` and `horizon_noveg.tif` bit for
bit, which is how you know the rebuild is the right cube and not a plausible
one. That rebuild is `rebuild_s3_reference.py`, and it is in this directory
precisely so the next accident is a two-minute fix.

The rule it leaves: **a reference cube has to be regenerable, and the command
that regenerates it belongs in the table above.** A cube nobody can rebuild is
not a reference, it is a rumour with a checksum.

### What still runs, and what does not

A bench script measures the engine of the day it was written, and S2-S4 changed
that engine on purpose. So the general rule first: **anything that sweeps the
city produces a cube of today's engine, not of the one it was written to
measure.** `data/bench/montilla-test-real/v1` is the clearest case -- it holds
the pre-S3 distance convention, and nothing in this directory can build it
again, because the code that did was replaced in S3.

Four scripts go further and no longer reproduce their own numbers at all. Each
says so in its own docstring:

- `bench_s3_sweep.py` -- it re-labels whatever `sector_offsets` returns, and S3
  replaced that with the DDA traversal, so `nominal` now hands back the new
  convention instead of the old one.
- `bench_s4_blockers.py`, `bench_s4_sweep.py`, `bench_s4_timing.py` -- they
  measured `step_mode="geometric"`, retired in `40fe8fc`.

They stay as the record of how those figures were obtained. Re-running them
means reverting the commit that made them obsolete, which is the honest price of
the question.

Everything else runs against the current code. `verify_s3_arbiter.py` and
`verify_s4.py` are the exit gates of their sessions, and the two worth
re-running after touching `raycast.py` or `horizon.py`.

## Index

**Inputs.** `prep_stack.py`, `prep_stack_wide.py` build the padded raster stacks
from the LAZ so every session measures the same real data.

**S1, where the sweep spends its time.** `bench_kernel.py` (the kernel on real
data), `bench_float64.py` (whether the tangent identity is exact before the
float32 cast), `sweep_parity.py` (the exit criterion: the whole city, compared
against its own COGs).

**S2, float32.** `bench_s2_sweep.py` (sweep per precision), `bench_s2_verdict.py`
(what it does to the verdict the product ships), `bench_s2_ties.py` (is every
blocker change a tie), `bench_s2_truth.py` (of the pixels that flip, which
precision is right), `bench_s2_bias.py` and `bench_s2_datum.py` (how much is a
fixable bias), `bench_s2_rss.py` (real memory, not the model),
`bench_s2_final.py` (the shipped code, not a copy of it).

**S3, the distance convention.** `bench_s3_offsets.py` (the geometric bias,
exactly), `bench_s3_sweep.py` (a sweep per convention), `bench_s3_verdict.py`,
`bench_s3_dda.py` (an arbiter from outside the family), `bench_s3_arbiter.py`,
`bench_s3_interval.py` (the arbiter as an interval, and where the residual
lives), `verify_s3_arbiter.py`, `rebuild_s3_reference.py` (the reference cube,
swept with the tie-break the S3 engine really had).

**S5, the seasonal canopy.** `bench_s5_canopy_interval.py` (three scenes --
opaque, geometric, felled -- to separate the opaque-canopy rule from the
vegetation in the cube, in hours of shade per day over the declination ladder),
`bench_s5_sanity.py` (is montilla-test's canopy typical, and how big is the
horizon drop really).

**S4, radius and step.** `bench_s4_timing.py` (the variants timed on a quiet
machine), `bench_s4_sweep.py`, `bench_s4_verdict.py`, `bench_s4_blockers.py` (of
the cells `geometric` skips, how many are the only blocker), `bench_s4_radius.py`
(at equal error, which lever is cheaper), `bench_s4_lowsun.py` (is cutting the
radius free where it costs), `bench_s4_diagonals.py`, `bench_s4_corners.py`,
`bench_s4_corner_verdict.py` and `bench_s4_tiebreak.py` (the corner tie, from
what it decides to which uniform rule wins), `probe_s4_tie.py` (does the
tie-break bench reproduce the production sweep plane for plane -- it does),
`bench_s4_farfield.py` (does the far field move sunset boundaries),
`bench_s4_edges.py` (a fraction without a magnitude decides nothing), `bench_s4_cordoba_sierra.py` (what Sierra Morena
subtends from Cordoba, on public MDT25), `verify_s4.py`.
