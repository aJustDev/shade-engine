"""/v1/shade/timeline: intervals, shaded_until and cache semantics."""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from shade_api.routes import resolve_at, shaded_until
from shade_core.artifacts import load_scene
from shade_core.shade import ShadeInterval, ShadeState, ShadeType, shade_timeline
from test_api_shade import _NEAR_X, _NEAR_Y, NEAR_LAT, NEAR_LON

TZ = ZoneInfo("Europe/Madrid")
WINTER_DAY = date(2026, 12, 21)


def test_winter_timeline_matches_engine(client: TestClient, built_city: Path) -> None:
    response = client.get(
        "/v1/shade/timeline",
        params={"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON, "date": str(WINTER_DAY)},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    body = response.json()
    assert body["date"] == str(WINTER_DAY)
    assert body["timezone"] == "Europe/Madrid"
    assert body["shaded_until"] is None  # not today

    expected = shade_timeline(
        load_scene(built_city), _NEAR_X, _NEAR_Y, NEAR_LAT, NEAR_LON, WINTER_DAY, TZ
    )
    assert body["intervals"] == [
        {
            "from": interval.start.strftime("%H:%M"),
            "to": interval.end.strftime("%H:%M"),
            "state": interval.state.value,
            "in_shade": interval.state is ShadeState.SHADE,
            "shade_type": interval.shade_type.value if interval.shade_type else None,
        }
        for interval in expected
    ]
    states = {interval["state"] for interval in body["intervals"]}
    assert states == {"sun", "shade"}  # the cube shades NEAR at winter midday


def test_omitted_date_means_today(client: TestClient) -> None:
    response = client.get(
        "/v1/shade/timeline", params={"city": "cube", "lat": NEAR_LAT, "lon": NEAR_LON}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.json()["date"] == datetime.now(TZ).date().isoformat()


def _interval(
    start_hour: float,
    end_hour: float,
    state: ShadeState,
    shade_type: ShadeType | None = None,
) -> ShadeInterval:
    base = datetime(2026, 7, 10, 0, 0, tzinfo=TZ)
    return ShadeInterval(
        start=base + timedelta(hours=start_hour),
        end=base + timedelta(hours=end_hour),
        state=state,
        shade_type=shade_type,
    )


def test_shaded_until_single_interval() -> None:
    intervals = [
        _interval(8, 11, ShadeState.SUN),
        _interval(11, 14, ShadeState.SHADE, ShadeType.BUILDING),
        _interval(14, 20, ShadeState.SUN),
    ]
    now = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)
    assert shaded_until(intervals, now) == intervals[1].end


def test_shaded_until_merges_contiguous_shade_run() -> None:
    """Building shade rolling into vegetation shade is one shaded run."""
    intervals = [
        _interval(8, 11, ShadeState.SHADE, ShadeType.BUILDING),
        _interval(11, 13, ShadeState.SHADE, ShadeType.VEGETATION),
        _interval(13, 20, ShadeState.SUN),
    ]
    now = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)
    assert shaded_until(intervals, now) == intervals[1].end


def test_shaded_until_in_sun_is_none() -> None:
    intervals = [
        _interval(8, 11, ShadeState.SUN),
        _interval(11, 14, ShadeState.SHADE, ShadeType.BUILDING),
    ]
    now = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)
    assert shaded_until(intervals, now) is None


def test_shaded_until_at_night_is_none() -> None:
    intervals = [_interval(8, 20, ShadeState.SUN)]
    now = datetime(2026, 7, 10, 23, 0, tzinfo=TZ)
    assert shaded_until(intervals, now) is None


def test_resolve_at_rules() -> None:
    explicit = datetime(2026, 12, 21, 13, 20)
    resolved = resolve_at(explicit, TZ)
    assert resolved.tzinfo == TZ and resolved.hour == 13

    aware = datetime(2026, 12, 21, 12, 20, tzinfo=ZoneInfo("UTC"))
    assert resolve_at(aware, TZ) == aware  # same instant
    assert resolve_at(aware, TZ).utcoffset() == timedelta(hours=1)  # city offset

    assert abs(resolve_at(None, TZ) - datetime.now(TZ)) < timedelta(seconds=5)
