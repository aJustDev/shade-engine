"""Cadastral footprints over WFS: the parse, the window grid and the joins.

The service is the part of this that cannot be tested by reasoning about it, so
the document below is a real answer from ``wfsBU.aspx`` with the boilerplate
trimmed and nothing else touched -- namespaces, nesting and axis order included.
"""

from pathlib import Path

import httpx
import pytest
from shapely.geometry import Polygon

from shade_pipeline.cadastre import (
    CadastreError,
    CadastreSource,
    _buildings,
    _windows,
)

# Verbatim from the service, over Montalban de Cordoba: two nested namespaces,
# the geometry three levels down, and coordinates as easting/northing because
# the request named EPSG:25830.
REAL_ANSWER = b"""<?xml version="1.0" encoding="ISO-8859-1"?>
<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"
  xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:bu-ext2d="http://inspire.jrc.ec.europa.eu/schemas/bu-ext2d/2.0"
  xmlns:bu-core2d="http://inspire.jrc.ec.europa.eu/schemas/bu-core2d/2.0">
 <member>
  <bu-ext2d:Building gml:id="ES.SDGC.BU.14040A01400111">
    <bu-core2d:conditionOfConstruction>functional</bu-core2d:conditionOfConstruction>
    <bu-ext2d:geometry>
     <bu-core2d:BuildingGeometry>
      <bu-core2d:geometry>
       <gml:Surface gml:id="Surface_1" srsName="urn:ogc:def:crs:EPSG::25830">
         <gml:patches>
          <gml:PolygonPatch>
           <gml:exterior>
            <gml:LinearRing>
             <gml:posList srsDimension="2" count="5">345138.36 4160819.06 345137.4 \
4160817.02 345131.8 4160819.64 345132.76 4160821.69 345138.36 4160819.06</gml:posList>
            </gml:LinearRing>
           </gml:exterior>
          </gml:PolygonPatch>
         </gml:patches>
       </gml:Surface>
      </bu-core2d:geometry>
     </bu-core2d:BuildingGeometry>
    </bu-ext2d:geometry>
  </bu-ext2d:Building>
 </member>
</FeatureCollection>
"""

BBOX_EXCEEDED = b"""<?xml version='1.0' encoding="ISO-8859-1" standalone="no"?>
<ExceptionReport xmlns="http://www.opengis.net/ows/1.1" version="2.0.0">
<Exception exceptionCode="OperationProcessingFailed">
<ExceptionText><![CDATA[Area of extension out of limits]]></ExceptionText>
</Exception>
</ExceptionReport>
"""


def _square(identifier: str, x: float, y: float, side: float = 10.0) -> str:
    ring = " ".join(
        f"{px} {py}"
        for px, py in [
            (x, y),
            (x + side, y),
            (x + side, y + side),
            (x, y + side),
            (x, y),
        ]
    )
    return f"""<member><bu-ext2d:Building gml:id="{identifier}"><bu-ext2d:geometry>
      <bu-core2d:BuildingGeometry><bu-core2d:geometry><gml:Surface><gml:patches>
      <gml:PolygonPatch><gml:exterior><gml:LinearRing>
      <gml:posList srsDimension="2">{ring}</gml:posList>
      </gml:LinearRing></gml:exterior></gml:PolygonPatch>
      </gml:patches></gml:Surface></bu-core2d:geometry></bu-core2d:BuildingGeometry>
      </bu-ext2d:geometry></bu-ext2d:Building></member>"""


def _collection(*members: str) -> bytes:
    return (
        '<?xml version="1.0"?><FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" '
        'xmlns:gml="http://www.opengis.net/gml/3.2" '
        'xmlns:bu-ext2d="http://inspire.jrc.ec.europa.eu/schemas/bu-ext2d/2.0" '
        'xmlns:bu-core2d="http://inspire.jrc.ec.europa.eu/schemas/bu-core2d/2.0">'
        + "".join(members)
        + "</FeatureCollection>"
    ).encode()


def test_a_real_answer_parses_into_a_footprint_in_easting_northing() -> None:
    """The axis-order trap, fixed as a value rather than as a comment.

    Read the other way round this ring would land off the coast of Somalia, and
    nothing downstream would complain: it is a valid polygon either way.
    """
    found = _buildings(REAL_ANSWER)

    (identifier,) = found
    assert identifier == "ES.SDGC.BU.14040A01400111"
    x, y = found[identifier].exterior.coords[0]
    assert 345000 < x < 346500, "easting first"
    assert 4159000 < y < 4162000, "northing second"
    assert found[identifier].area == pytest.approx(13.97, abs=0.01)


def test_a_patio_arrives_as_a_hole() -> None:
    """An interior ring is a courtyard, and a courtyard filled in is a lie."""
    with_patio = _collection(
        """<member><bu-ext2d:Building gml:id="X"><bu-ext2d:geometry>
        <bu-core2d:BuildingGeometry><bu-core2d:geometry><gml:Surface><gml:patches>
        <gml:PolygonPatch>
        <gml:exterior><gml:LinearRing><gml:posList srsDimension="2">
        0 0 30 0 30 30 0 30 0 0</gml:posList></gml:LinearRing></gml:exterior>
        <gml:interior><gml:LinearRing><gml:posList srsDimension="2">
        10 10 20 10 20 20 10 20 10 10</gml:posList></gml:LinearRing></gml:interior>
        </gml:PolygonPatch></gml:patches></gml:Surface></bu-core2d:geometry>
        </bu-core2d:BuildingGeometry></bu-ext2d:geometry></bu-ext2d:Building></member>"""
    )

    (footprint,) = _buildings(with_patio).values()

    assert len(footprint.interiors) == 1
    assert footprint.area == pytest.approx(30 * 30 - 10 * 10)


def test_the_window_grid_leaves_no_gap() -> None:
    """A hole in the grid is a street of missing buildings nobody would notice."""
    bbox = (0.0, 0.0, 1048.0, 1954.0)

    windows = list(_windows(bbox, 500.0))

    assert len(windows) == 3 * 4
    covered = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in windows)
    assert covered == pytest.approx(1048.0 * 1954.0)
    assert max(x1 for _x0, _y0, x1, _y1 in windows) == 1048.0
    assert max(y1 for _x0, _y0, _x1, y1 in windows) == 1954.0


def test_a_building_on_the_seam_is_fetched_twice_and_drawn_once() -> None:
    """Which is the whole reason the join is by ``gml:id`` and not by count.

    A WFS bbox filter returns what *intersects*, so the two windows either side
    of a wall both answer with it. Counting features would double it; the id
    makes the copy free.
    """
    seam = _collection(_square("ES.SDGC.BU.SEAM", 495.0, 10.0))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=seam))
    source = CadastreSource(
        cache_dir=Path("/nonexistent"), client=httpx.Client(transport=transport)
    )

    footprints = source.fetch((0.0, 0.0, 1000.0, 500.0), "EPSG:25830")

    assert len(footprints) == 1


def test_a_window_the_service_refuses_costs_only_that_window() -> None:
    """Half a city drawn beats none: this layer is an aid, not an ingredient."""
    good = _collection(_square("ES.SDGC.BU.A", 10.0, 10.0))

    def answer(request: httpx.Request) -> httpx.Response:
        if "0.0,0.0" in str(request.url) or "0,0" in str(request.url):
            return httpx.Response(200, content=BBOX_EXCEEDED)
        return httpx.Response(200, content=good)

    source = CadastreSource(
        cache_dir=Path("/nonexistent"), client=httpx.Client(transport=httpx.MockTransport(answer))
    )

    assert source.fetch((0.0, 0.0, 1000.0, 500.0), "EPSG:25830") != []


def test_the_service_being_down_is_an_empty_layer_and_not_a_failed_build() -> None:
    """A cadastre outage must never stop a city from being rendered."""

    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    source = CadastreSource(
        cache_dir=Path("/nonexistent"), client=httpx.Client(transport=httpx.MockTransport(refuse))
    )

    assert source.fetch((0.0, 0.0, 400.0, 400.0), "EPSG:25830") == []


def test_the_bbox_cap_is_reported_in_the_services_own_words() -> None:
    """``Area of extension out of limits`` is the message that explains the grid."""
    with pytest.raises(CadastreError, match="Area of extension out of limits"):
        _buildings(BBOX_EXCEEDED)


def test_a_body_that_is_not_xml_is_not_silently_no_buildings() -> None:
    """An empty layer and a broken answer must not look the same from outside."""
    with pytest.raises(CadastreError, match="did not answer with XML"):
        _buildings(b"<html>proxy error</html")


def test_a_warm_cache_needs_no_network(tmp_path: Path) -> None:
    """What makes a rebuild repeatable, and a demo possible on a train."""
    source = CadastreSource(url="http://unreachable.invalid/wfs", cache_dir=tmp_path)
    (tmp_path / "bu-building-EPSG-25830-0-0-400-400.gml").write_bytes(
        _collection(_square("ES.SDGC.BU.CACHED", 10.0, 10.0))
    )

    footprints = source.fetch((0.0, 0.0, 400.0, 400.0), "EPSG:25830")

    assert len(footprints) == 1
    assert isinstance(footprints[0], Polygon)


def test_a_crs_the_service_cannot_be_asked_for_says_so() -> None:
    source = CadastreSource(cache_dir=Path("/nonexistent"))

    with pytest.raises(CadastreError, match="not an EPSG code"):
        source._download((0.0, 0.0, 10.0, 10.0), "ETRS89 / UTM 30N")
