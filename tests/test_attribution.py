import pytest

from app.attribution import (
    BOUNDARY,
    CONFLICT,
    INTERIOR,
    MATCHED,
    OUTSIDE,
    OVERLAP,
    SHARED_BOUNDARY,
    UNMATCHED,
    Attributor,
    CompartmentGeometry,
    HotspotPoint,
)
from app.parsing import InputError, parse_hotspots_csv, parse_utc


def box(code, minx, miny, maxx, maxy, seq=0):
    from shapely.geometry import box as make_box

    return CompartmentGeometry(seq=seq, code=code, geometry=make_box(minx, miny, maxx, maxy))


def point(seq, lon, lat, no=None):
    return HotspotPoint(seq, seq + 2, no or f"H-{seq}", lon, lat, "2026-03-01T00:00:00Z")


@pytest.fixture
def three_boxes():
    return [box("A", 0, 0, 1, 1, 0), box("B", 1, 0, 2, 1, 1), box("C", 0, 1, 1, 2, 2)]


def test_interior_single_match(three_boxes):
    result = Attributor(three_boxes).attribute_one(point(0, 0.5, 0.5))
    assert result.status == MATCHED
    assert result.reason_kind == INTERIOR
    assert result.codes == ("A",)


def test_boundary_of_single_polygon_is_matched(three_boxes):
    result = Attributor(three_boxes).attribute_one(point(0, 0.0, 0.5))
    assert result.status == MATCHED
    assert result.reason_kind == BOUNDARY
    assert result.codes == ("A",)


def test_shared_edge_is_conflict(three_boxes):
    result = Attributor(three_boxes).attribute_one(point(0, 1.0, 0.5))
    assert result.status == CONFLICT
    assert result.reason_kind == SHARED_BOUNDARY
    assert result.codes == ("A", "B")
    assert "共边" in result.reason


def test_shared_corner_is_conflict_with_all_four(three_boxes):
    result = Attributor(three_boxes).attribute_one(point(0, 1.0, 1.0))
    assert result.status == CONFLICT
    assert result.reason_kind == SHARED_BOUNDARY
    assert result.codes == ("A", "B", "C")


def test_overlap_area_is_conflict():
    compartments = [box("A", 0, 0, 2, 2, 0), box("B", 1, 0, 3, 2, 1)]
    result = Attributor(compartments).attribute_one(point(0, 1.5, 1.0))
    assert result.status == CONFLICT
    assert result.reason_kind == OVERLAP
    assert "重叠" in result.reason


def test_outside_is_unmatched(three_boxes):
    result = Attributor(three_boxes).attribute_one(point(0, 5, 5))
    assert result.status == UNMATCHED
    assert result.reason_kind == OUTSIDE
    assert result.codes == ()


def test_no_compartments(three_boxes):
    result = Attributor([]).attribute_one(point(0, 0.5, 0.5))
    assert result.status == UNMATCHED


def test_result_independent_of_feature_order(three_boxes):
    forward = Attributor(three_boxes).attribute_one(point(0, 1.0, 0.5))
    reverse = Attributor(list(reversed(three_boxes))).attribute_one(point(0, 1.0, 0.5))
    assert forward.codes == reverse.codes == ("A", "B")
    assert forward.reason_kind == reverse.reason_kind


def test_duplicate_source_rows_are_all_reported(three_boxes):
    hotspots = [point(0, 0.5, 0.5), point(1, 0.5, 0.5)]
    results = Attributor(three_boxes).attribute(hotspots)
    assert len(results) == 2
    assert all(item.status == MATCHED for item in results)


def test_geometry_boundary_within_tolerance_is_not_snapped(three_boxes):
    """A point 1e-9 outside is outside; GEOS predicates must stay exact."""

    result = Attributor(three_boxes).attribute_one(point(0, 1.0 + 1e-9, 0.5))
    assert result.status == MATCHED
    assert result.codes == ("B",)


def test_timestamp_normalisation():
    assert parse_utc("2026-03-01T02:41:30+00:00") == "2026-03-01T02:41:30Z"
    assert parse_utc("2026-03-01 02:41:30") == "2026-03-01T02:41:30Z"
    assert parse_utc("2026-03-01T10:41:30+08:00") == "2026-03-01T02:41:30Z"
    assert parse_utc("2026-03-01T02:41Z") == "2026-03-01T02:41:00Z"
    with pytest.raises(InputError):
        parse_utc("not a time")


def test_csv_validation_rejects_bad_rows():
    header = "hotspot_no,lon,lat,observed_at\n"
    with pytest.raises(InputError, match="经度"):
        parse_hotspots_csv((header + "H-1,181,25,2026-01-01T00:00:00Z\n").encode(), "x.csv")
    with pytest.raises(InputError, match="热点号"):
        parse_hotspots_csv((header + ",100,25,2026-01-01T00:00:00Z\n").encode(), "x.csv")
    with pytest.raises(InputError, match="第 2 行"):
        parse_hotspots_csv((header + "H-1,100,25,xx\n").encode(), "x.csv")


def test_csv_accepts_chinese_headers_and_extra_columns():
    data = "热点号,经度,纬度,UTC时刻,置信度\nH-1,100.1,25.1,2026-01-01T00:00:00Z,high\n".encode()
    rows = parse_hotspots_csv(data, "x.csv")
    assert rows[0].raw["置信度"] == "high"
    assert rows[0].lon == 100.1
