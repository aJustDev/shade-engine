"""City registry: which cities the API can answer for, resolved at startup.

A city is servable when its YAML config has built artifacts under
``artifacts_root/<id>/<version>/``. Configs without artifacts are skipped
with a warning -- the normal state of a city whose pipeline has not run yet.
Artifacts that exist but violate the georeference contract abort startup: a
corrupt build is a bug, not a configuration state.
"""

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pyproj import Transformer

from shade_api.routing import RouteGraph
from shade_api.settings import ApiSettings
from shade_core.artifacts import METADATA_FILENAME, BuildMetadata, SceneReader
from shade_core.config import CityConfig, load_city
from shade_core.routegraph import load_route_graph

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CityRuntime:
    """Everything needed to answer queries about one city."""

    config: CityConfig
    metadata: BuildMetadata
    reader: SceneReader
    to_projected: Transformer  # EPSG:4326 -> city CRS, always_xy (lon, lat) in
    bbox_wgs84: tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
    tz: ZoneInfo
    route_graph: RouteGraph | None  # None until `shade-engine graph <city>` runs


class CityRegistry:
    """Servable cities by id; built once per process, closed on shutdown."""

    def __init__(self, cities: dict[str, CityRuntime]) -> None:
        self._cities = cities

    @classmethod
    def load(cls, settings: ApiSettings) -> CityRegistry:
        cities: dict[str, CityRuntime] = {}
        for path in sorted(settings.cities_dir.glob("*.yaml")):
            config = load_city(path)
            artifact_dir = settings.artifacts_root / config.id / settings.artifact_version
            if not (artifact_dir / METADATA_FILENAME).exists():
                logger.warning("skipping city %s: no artifacts at %s", config.id, artifact_dir)
                continue
            reader = SceneReader(
                artifact_dir,
                block_size=settings.block_size,
                max_blocks=settings.max_cached_blocks,
            )
            metadata = reader.metadata
            if metadata.crs != config.crs:
                raise ValueError(
                    f"{config.id}: artifact CRS {metadata.crs} does not match "
                    f"config CRS {config.crs}"
                )
            # One transformer per city, compiled once and shared: pyproj
            # transformers are thread-safe since 3.1. always_xy makes both
            # ends (x, y) ordered, i.e. transform(lon, lat) -> (x, y).
            to_projected = Transformer.from_crs("EPSG:4326", metadata.crs, always_xy=True)
            min_x, min_y, max_x, max_y = metadata.bbox
            # transform_bounds (not the two corners): straight UTM edges are
            # curves in lat/lon, so it densifies the edges before bounding.
            bbox_wgs84 = to_projected.transform_bounds(
                min_x, min_y, max_x, max_y, direction="INVERSE"
            )
            # The pedestrian graph is optional (a normal state for a city
            # whose `shade-engine graph` never ran: routes answer 503). A
            # graph that exists but fails its coherence checks aborts
            # startup, same policy as a corrupt raster artifact.
            try:
                route_graph = RouteGraph.build(load_route_graph(artifact_dir))
            except FileNotFoundError:
                logger.info("city %s has no pedestrian graph; routes disabled", config.id)
                route_graph = None
            cities[config.id] = CityRuntime(
                config=config,
                metadata=metadata,
                reader=reader,
                to_projected=to_projected,
                bbox_wgs84=bbox_wgs84,
                tz=ZoneInfo(config.timezone),
                route_graph=route_graph,
            )
        return cls(cities)

    def get(self, city_id: str) -> CityRuntime:
        """The runtime for a city id; raises KeyError for unknown cities."""
        return self._cities[city_id]

    def all(self) -> list[CityRuntime]:
        return [self._cities[city_id] for city_id in sorted(self._cities)]

    def close(self) -> None:
        for runtime in self._cities.values():
            runtime.reader.close()
