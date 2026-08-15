"""The memory guardrail: what the machine offers vs what a sweep worker costs."""

from pathlib import Path

import pytest

from shade_pipeline import budget
from shade_pipeline.budget import (
    HEADROOM,
    MemoryBudgetError,
    check_worker_budget,
    cpu_budget,
    estimate_result_bytes,
    estimate_worker_bytes,
)

GIB = 2**30
# The configuration the estimate was calibrated against: a real Cordoba tile.
REAL_TILE = (64, 512, 500)


def test_estimate_covers_the_measured_worker() -> None:
    """The model must sit above the 87 MiB a real tile actually grew by.

    Erring high is the point of a guardrail, but not by so much that it
    refuses sound runs, so the band is pinned on both sides.
    """
    estimate = estimate_worker_bytes(*REAL_TILE)
    assert 87 * 2**20 < estimate < 2 * 87 * 2**20


def test_estimate_scales_with_the_knobs() -> None:
    """Halving the tile is the lever that makes more workers fit."""
    sectors, tile, pad = REAL_TILE
    assert estimate_worker_bytes(sectors, tile // 2, pad) < estimate_worker_bytes(*REAL_TILE)
    assert estimate_worker_bytes(sectors * 2, tile, pad) > estimate_worker_bytes(*REAL_TILE)
    assert estimate_result_bytes(sectors, tile) == 3 * sectors * tile * tile


def test_budget_accepts_what_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget, "available_bytes", lambda: 8 * GIB)
    check_worker_budget(3, *REAL_TILE)  # must not raise


def test_budget_refuses_and_names_what_would_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    per_worker = estimate_worker_bytes(*REAL_TILE) + estimate_result_bytes(64, 512)
    # Room for exactly two workers once the headroom is taken out.
    monkeypatch.setattr(budget, "available_bytes", lambda: int(2 * per_worker / HEADROOM))
    check_worker_budget(2, *REAL_TILE)
    with pytest.raises(MemoryBudgetError, match="--workers 2 or fewer"):
        check_worker_budget(6, *REAL_TILE)


def test_unreadable_budget_never_blocks_a_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine that will not say how much memory it has is not a veto."""
    monkeypatch.setattr(budget, "available_bytes", lambda: None)
    check_worker_budget(64, *REAL_TILE)  # must not raise


def test_available_bytes_reads_this_machine() -> None:
    """Whatever this box is, the answer is a plausible number or an honest None."""
    available = budget.available_bytes()
    assert available is None or available > 0


def test_cpu_budget_is_at_least_one() -> None:
    assert cpu_budget() >= 1


def test_cgroup_quota_beats_affinity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``cpus: 3`` in compose is a CFS quota, invisible to the affinity mask."""
    monkeypatch.setattr(budget, "_read_cgroup", lambda name: "300000 100000")
    assert cpu_budget() == 3
    monkeypatch.setattr(budget, "_read_cgroup", lambda name: "max 100000")
    assert cpu_budget() == len(__import__("os").sched_getaffinity(0))


def test_cgroup_available_discounts_the_page_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binning leaves gigabytes of reclaimable cache in ``memory.current``.

    Counting it as used would refuse a sweep that has all the room it needs.
    """
    files = {
        "memory.max": str(6 * GIB),
        "memory.current": str(5 * GIB),
        "memory.stat": f"anon {GIB}\nfile {4 * GIB}\nkernel 0",
    }
    monkeypatch.setattr(budget, "_read_cgroup", files.get)
    assert budget._cgroup_available() == 5 * GIB


def test_unlimited_cgroup_falls_back_to_meminfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget, "_read_cgroup", lambda name: "max")
    assert budget._cgroup_available() is None
