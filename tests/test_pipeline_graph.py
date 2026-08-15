"""Pedestrian graph artifact: extraction, sampling, fractions, CLI."""

import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import yaml
from rasterio.transform import from_origin
from typer.testing import CliRunner

import graph_fixture
import synthetic
from conftest import CUBE_CITY
from shade_core.routegraph import (
    GRAPH_DIRNAME,
    OSM_ATTRIBUTION,
    ROUTE_GRAPH_SCHEMA_VERSION,
    RouteGraphArtifact,
    RouteGraphMeta,
    load_route_graph,
)
from shade_core.solar import sun_position
from shade_pipeline.cli import app
from shade_pipeline.graph import (
    GraphArrays,
    OsmnxWalkSource,
    build_graph,
    edge_state_fractions,
    extract_graph_arrays,
    graph_ladder,
    sample_edges,
    write_route_graph,
)
from shade_pipeline.shade_raster import (
    STATE_OUTSIDE,
    STATE_SHADE_BOTH,
    STATE_SHADE_BUILDING,
    STATE_SHADE_VEGETATION,
)
from shade_pipeline.tiles import season_preset_instants

CRS = "EPSG:25830"
TZ = ZoneInfo("Europe/Madrid")


def _local(x: float, y: float) -> tuple[float, float]:
    return synthetic.UTM_ORIGIN[0] + x, synthetic.UTM_ORIGIN[1] + y


# --- extraction ---------------------------------------------------------------


def test_extract_dedups_reciprocals_and_keeps_parallels() -> None:
    arrays = extract_graph_arrays(graph_fixture.cube_walk_graph(), CRS)
    assert len(arrays.node_x) == 4
    assert len(arrays.edge_len) == 5  # 7 directed edges -> 5 undirected
    pairs = sorted(zip(arrays.edge_u.tolist(), arrays.edge_v.tolist(), strict=True))
    # Nodes sort as ids 1..4 -> indices 0..3; the north pair (3, 4) keeps
    # its straight edge AND its arc.
    assert pairs == [(0, 1), (0, 2), (1, 3), (2, 3), (2, 3)]
    north = sorted(
        length
        for u, v, length in zip(
            arrays.edge_u.tolist(), arrays.edge_v.tolist(), arrays.edge_len.tolist(), strict=True
        )
        if (u, v) == (2, 3)
    )
    assert north[0] == pytest.approx(12.0, abs=0.05)  # straight
    assert north[1] == pytest.approx(2 * np.hypot(6.0, 6.0), abs=0.05)  # arc via (60, 94)


def test_extract_drops_self_loops_and_isolated_nodes() -> None:
    graph = graph_fixture.cube_walk_graph()
    graph.add_edge(1, 1)  # self-loop
    lon, lat = graph_fixture.lonlat((25.0, 25.0))
    graph.add_node(99, x=lon, y=lat)  # isolated node
    arrays = extract_graph_arrays(graph, CRS)
    assert len(arrays.node_x) == 4
    assert len(arrays.edge_len) == 5


def test_extract_recomputes_length_from_projected_geometry() -> None:
    arrays = extract_graph_arrays(graph_fixture.cube_walk_graph(), CRS)
    for edge in range(len(arrays.edge_len)):
        xs = arrays.geom_x[arrays.geom_offsets[edge] : arrays.geom_offsets[edge + 1]]
        ys = arrays.geom_y[arrays.geom_offsets[edge] : arrays.geom_offsets[edge + 1]]
        arc = float(np.sum(np.hypot(np.diff(xs), np.diff(ys))))
        assert arrays.edge_len[edge] == pytest.approx(arc, rel=1e-6)


# --- sampling -----------------------------------------------------------------


def test_sample_edges_spacing_and_endpoints() -> None:
    graph = graph_fixture.cube_walk_graph()
    sample_x, sample_y, sample_edge = sample_edges(extract_graph_arrays(graph, CRS), 5.0)
    arrays = extract_graph_arrays(graph, CRS)
    for edge in range(len(arrays.edge_len)):
        mask = sample_edge == edge
        xs, ys = sample_x[mask], sample_y[mask]
        total = float(arrays.edge_len[edge])
        assert len(xs) == max(int(total // 5.0) + 2, 2)
        # Endpoints included, and consecutive samples evenly spaced <= 5 m
        # along the polyline (straight edges: also in euclidean distance).
        first = arrays.geom_offsets[edge]
        last = arrays.geom_offsets[edge + 1] - 1
        assert (xs[0], ys[0]) == pytest.approx((arrays.geom_x[first], arrays.geom_y[first]))
        assert (xs[-1], ys[-1]) == pytest.approx((arrays.geom_x[last], arrays.geom_y[last]))
        steps = np.hypot(np.diff(xs), np.diff(ys))
        assert steps.max() <= 5.0 + 1e-9


# --- fractions against a hand raster ------------------------------------------


def test_edge_state_fractions_states_and_bounds() -> None:
    # 10x10 raster, origin (0, 10): west half sun, east half building shade,
    # one OUTSIDE pixel in the shaded half (counts as sun).
    state = np.zeros((10, 10), dtype=np.uint8)
    state[:, 5:] = STATE_SHADE_BUILDING
    state[2, 7] = STATE_OUTSIDE
    transform = from_origin(0.0, 10.0, 1.0, 1.0)

    # Edge 0: ten samples across the middle row -> 5 sun + 5 shade.
    # Edge 1: five samples, all outside the grid -> all sun (conservative);
    # edge 2: two samples on the OUTSIDE pixel -> sun.
    sample_x = np.array([0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, *[50.0] * 5, 7.5, 7.5])
    sample_y = np.array([*[4.5] * 10, *[4.5] * 5, 7.5, 7.5])
    sample_edge = np.array([*[0] * 10, *[1] * 5, 2, 2], dtype=np.int64)
    fractions, vegetation = edge_state_fractions(
        sample_x, sample_y, sample_edge, 3, state, transform
    )
    assert fractions[0] == pytest.approx(0.5)
    assert fractions[1] == 1.0
    assert fractions[2] == 1.0
    # No canopy anywhere in this raster, not even off-grid.
    assert vegetation.tolist() == [0.0, 0.0, 0.0]


def test_edge_state_fractions_splits_vegetation() -> None:
    """Building shade and canopy are both 'not sun' but must not be mixed."""
    state = np.zeros((10, 10), dtype=np.uint8)
    state[:, 2:6] = STATE_SHADE_BUILDING
    state[:, 6:] = STATE_SHADE_VEGETATION
    transform = from_origin(0.0, 10.0, 1.0, 1.0)

    # Ten samples across one row: 2 sun, 4 building shade, 4 canopy.
    sample_x = np.array([0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5])
    sample_y = np.array([4.5] * 10)
    sample_edge = np.zeros(10, dtype=np.int64)
    fractions, vegetation = edge_state_fractions(
        sample_x, sample_y, sample_edge, 1, state, transform
    )
    assert fractions[0] == pytest.approx(0.2)
    assert vegetation[0] == pytest.approx(0.4)
    # What is left is shade cast by buildings or terrain.
    assert 1.0 - fractions[0] - vegetation[0] == pytest.approx(0.4)


def test_edge_state_fractions_count_both_as_vegetation() -> None:
    """Shade under a crown counts as canopy even when a wall also casts it.

    The router's question is comfort -- is there a crown over me -- not the
    counterfactual the map layers answer, so BOTH belongs on this side.
    """
    state = np.zeros((10, 10), dtype=np.uint8)
    state[:, 2:6] = STATE_SHADE_BOTH
    state[:, 6:] = STATE_SHADE_VEGETATION
    transform = from_origin(0.0, 10.0, 1.0, 1.0)

    sample_x = np.array([0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5])
    sample_y = np.array([4.5] * 10)
    sample_edge = np.zeros(10, dtype=np.int64)
    fractions, vegetation = edge_state_fractions(
        sample_x, sample_y, sample_edge, 1, state, transform
    )
    assert fractions[0] == pytest.approx(0.2)
    assert vegetation[0] == pytest.approx(0.8)


# --- ladder columns -----------------------------------------------------------


def test_graph_ladder_columns_match_season_preset() -> None:
    ladder = graph_ladder()
    instants = season_preset_instants(TZ)
    columns = [(rung.date, column.time, column.col) for rung in ladder for column in rung.columns]
    assert [col for _, _, col in columns] == list(range(len(instants)))
    for (day, hhmm, _col), when in zip(columns, instants, strict=True):
        assert when.date().isoformat() == day
        assert when.strftime("%H:%M") == hhmm


# --- artifact roundtrip -------------------------------------------------------


def _meta_for(arrays: GraphArrays, samples: int) -> RouteGraphMeta:
    return RouteGraphMeta(
        schema_version=ROUTE_GRAPH_SCHEMA_VERSION,
        city_id="cube",
        crs=CRS,
        network_type="walk",
        spacing_m=5.0,
        nodes=len(arrays.node_x),
        edges=len(arrays.edge_len),
        samples=samples,
        built_at=datetime.fromisoformat("2026-08-14T00:00:00+00:00"),
        ladder=graph_ladder(),
        attribution=[OSM_ATTRIBUTION],
    )


def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    arrays = extract_graph_arrays(graph_fixture.cube_walk_graph(), CRS)
    rng = np.random.default_rng(7)
    edges = len(arrays.edge_len)
    fractions = rng.integers(0, 128, size=(edges, 83), dtype=np.uint8)
    vegetation = rng.integers(0, 128, size=(edges, 83), dtype=np.uint8)
    write_route_graph(tmp_path, arrays, fractions, vegetation, _meta_for(arrays, samples=123))

    stored = load_route_graph(tmp_path)
    assert isinstance(stored, RouteGraphArtifact)
    assert np.array_equal(stored.fractions, fractions)
    assert np.array_equal(stored.veg_fractions, vegetation)
    assert np.array_equal(stored.node_x, arrays.node_x)
    assert np.array_equal(stored.geom_offsets, arrays.geom_offsets)
    assert stored.meta.attribution == [OSM_ATTRIBUTION]


def test_load_without_graph_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="shade-engine graph"):
        load_route_graph(tmp_path)


def _write_zero_graph(tmp_path: Path) -> GraphArrays:
    arrays = extract_graph_arrays(graph_fixture.cube_walk_graph(), CRS)
    zeros = np.zeros((len(arrays.edge_len), 83), dtype=np.uint8)
    write_route_graph(tmp_path, arrays, zeros, zeros, _meta_for(arrays, samples=1))
    return arrays


def test_load_rejects_incoherent_fractions(tmp_path: Path) -> None:
    _write_zero_graph(tmp_path)
    # Corrupt one file after the fact: a wrong-shaped fractions matrix must
    # fail at load, mentioning what disagrees.
    np.savez(
        tmp_path / GRAPH_DIRNAME / "fractions.npz",
        sun_fraction=np.zeros((2, 83), dtype=np.uint8),
        veg_shade_fraction=np.zeros((2, 83), dtype=np.uint8),
    )
    with pytest.raises(ValueError, match="fractions shape"):
        load_route_graph(tmp_path)


def test_load_rejects_missing_veg_fractions(tmp_path: Path) -> None:
    """A schema 1 fractions file (sun only) must fail with a rebuild hint."""
    arrays = _write_zero_graph(tmp_path)
    np.savez(
        tmp_path / GRAPH_DIRNAME / "fractions.npz",
        sun_fraction=np.zeros((len(arrays.edge_len), 83), dtype=np.uint8),
    )
    with pytest.raises(ValueError, match="shade-engine graph"):
        load_route_graph(tmp_path)


def test_load_rejects_sun_plus_vegetation_above_one(tmp_path: Path) -> None:
    """Sun and canopy are disjoint states: their fractions cannot both be 1."""
    arrays = _write_zero_graph(tmp_path)
    full = np.full((len(arrays.edge_len), 83), 255, dtype=np.uint8)
    np.savez(
        tmp_path / GRAPH_DIRNAME / "fractions.npz",
        sun_fraction=full,
        veg_shade_fraction=full,
    )
    with pytest.raises(ValueError, match="sum above 1"):
        load_route_graph(tmp_path)


def test_load_rejects_old_schema_version(tmp_path: Path) -> None:
    arrays = _write_zero_graph(tmp_path)
    meta_path = tmp_path / GRAPH_DIRNAME / "graph.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["schema_version"] = 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert len(arrays.edge_len) > 0
    with pytest.raises(ValueError, match="schema_version"):
        load_route_graph(tmp_path)


# --- the physics golden over the built city -----------------------------------


def _column(meta: RouteGraphMeta, day: str, time: str) -> int:
    for rung in meta.ladder:
        if rung.date == day:
            for column in rung.columns:
                if column.time == time:
                    return column.col
    raise AssertionError(f"no ladder column for {day} {time}")


def _edge_at(artifact: RouteGraphArtifact, a: tuple[float, float], b: tuple[float, float]) -> int:
    """Edge index whose endpoints match two local-frame points (straight edges)."""
    ax, ay = _local(*a)
    bx, by = _local(*b)
    for edge in range(len(artifact.edge_len)):
        first = artifact.geom_offsets[edge]
        last = artifact.geom_offsets[edge + 1] - 1
        ends = {
            (round(float(artifact.geom_x[first]), 3), round(float(artifact.geom_y[first]), 3)),
            (round(float(artifact.geom_x[last]), 3), round(float(artifact.geom_y[last]), 3)),
        }
        expected = {(round(ax, 3), round(ay, 3)), (round(bx, 3), round(by, 3))}
        if last - first == 1 and ends == expected:
            return edge
    raise AssertionError(f"no straight edge between {a} and {b}")


def test_graph_is_clipped_to_the_computation_area(masked_city: Path, tmp_path: Path) -> None:
    """Streets the build never computed do not enter the graph at all.

    An uncovered sample scores as sun, so keeping those edges would hand a
    shade seeker a route down a street nobody measured, advertised as sunlit.
    The cube fixture's area covers the western half, so every edge that
    reaches east of the middle goes, and with it any node left dangling.
    """
    artifact_dir = tmp_path / "cube" / "v1"
    shutil.copytree(masked_city, artifact_dir)
    lines: list[str] = []
    build_graph(CUBE_CITY, artifact_dir, graph_fixture.SyntheticWalkSource(), progress=lines.append)

    artifact = load_route_graph(artifact_dir)
    assert artifact.meta.edges < 5  # the unclipped fixture has 5
    assert any("edges dropped" in line for line in lines)

    middle = (CUBE_CITY.bbox[0] + CUBE_CITY.bbox[2]) / 2.0
    assert (artifact.geom_x <= middle).all()
    assert (artifact.node_x <= middle).all()
    # The reindex has to leave the edges pointing at nodes that still exist.
    assert artifact.edge_u.max() < artifact.meta.nodes
    assert artifact.edge_v.max() < artifact.meta.nodes
    assert artifact.meta.nodes == len(artifact.node_x)


def test_build_graph_fractions_follow_the_cube_shadow(routed_city: Path) -> None:
    """The pocket edge flips shade (winter noon) -> sun (summer noon); the
    north edge stays sunny. Sun-fraction values are uint8 (255 = full sun)."""
    artifact = load_route_graph(routed_city)
    assert artifact.meta.city_id == "cube"
    assert artifact.meta.nodes == 4
    assert artifact.meta.edges == 5

    # Guard: the golden's margins assume these solar geometries. Winter noon
    # shadow reaches y = 50 + 18.4/tan(elev) in (82, 86); summer's stays
    # below y = 58. If pvlib or the ladder preset ever shifts these, fail
    # here, loudly, not in the fraction asserts below.
    center_lon, center_lat = graph_fixture.lonlat((60.0, 60.0))
    winter = sun_position(center_lat, center_lon, datetime(2026, 12, 21, 13, 0, tzinfo=TZ))
    summer = sun_position(center_lat, center_lon, datetime(2026, 6, 21, 13, 0, tzinfo=TZ))
    assert 27.5 < winter.elevation_deg < 29.5
    assert 165.0 < winter.azimuth_deg < 200.0
    assert summer.elevation_deg > 63.0

    col_winter = _column(artifact.meta, "2026-12-21", "13:00")
    col_summer = _column(artifact.meta, "2026-06-21", "13:00")
    pocket = _edge_at(artifact, graph_fixture.POCKET_A, graph_fixture.POCKET_B)
    north = _edge_at(artifact, graph_fixture.NORTH_A, graph_fixture.NORTH_B)

    assert artifact.fractions[pocket, col_winter] < 26  # deep in the cube's shadow
    assert artifact.fractions[pocket, col_summer] > 230  # same street, summer sun
    assert artifact.fractions[north, col_winter] > 230  # beyond the shadow tip


# --- CLI ----------------------------------------------------------------------


def _write_cube_yaml(cities_dir: Path) -> None:
    cities_dir.mkdir(parents=True, exist_ok=True)
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))


def test_cli_graph_requires_build(tmp_path: Path) -> None:
    _write_cube_yaml(tmp_path / "cities")
    result = CliRunner().invoke(
        app,
        ["graph", "cube", "--cities-dir", str(tmp_path / "cities"), "--output-root", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "run shade-engine build first" in result.output


def test_cli_graph_e2e(built_city: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_cube_yaml(tmp_path / "cities")
    output_root = tmp_path / "data"
    shutil.copytree(built_city, output_root / "cube" / "v1")
    monkeypatch.setattr(
        OsmnxWalkSource, "fetch", lambda self, bbox: graph_fixture.cube_walk_graph()
    )
    result = CliRunner().invoke(
        app,
        [
            "graph",
            "cube",
            "--cities-dir",
            str(tmp_path / "cities"),
            "--output-root",
            str(output_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "graph artifact written" in result.output
    stored = load_route_graph(output_root / "cube" / "v1")
    assert stored.meta.samples > 0
