"""Spatial attribution of hotspot points to compartments.

Rules (deliberately explicit, because the warehouse depends on them):

* A hotspot belongs to a compartment when it is **inside or on the boundary**
  of exactly one compartment.
* If it touches more than one compartment it is a ``conflict``.  Two kinds are
  reported: ``conflict_shared_boundary`` when the point lies on the boundary of
  every candidate (a shared edge/vertex) and ``conflict_overlap`` when at least
  one candidate contains it in its interior (overlapping polygons).
* No candidate -> ``unmatched``.

Point-in-polygon uses exact predicates from GEOS (``covers`` includes the
boundary, ``contains`` excludes it), so a hotspot placed exactly on a shared
edge is always reported as a conflict rather than being assigned by ordering.
Candidate lookup is bbox-only through an STRtree; the precise test is always
``covers``.  This makes the result independent of feature order in the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from shapely import prepared
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

MATCHED = "matched"
CONFLICT = "conflict"
UNMATCHED = "unmatched"

INTERIOR = "matched_interior"
BOUNDARY = "matched_boundary"
OVERLAP = "conflict_overlap"
SHARED_BOUNDARY = "conflict_shared_boundary"
OUTSIDE = "unmatched_outside"

STATUS_LABELS = {
    MATCHED: "已赋码",
    CONFLICT: "归属冲突",
    UNMATCHED: "未匹配",
}

REASON_LABELS = {
    INTERIOR: "唯一落入",
    BOUNDARY: "边界赋码",
    OVERLAP: "重叠区",
    SHARED_BOUNDARY: "共边",
    OUTSIDE: "区外",
}


@dataclass(frozen=True)
class HotspotPoint:
    seq: int
    source_row: int
    hotspot_no: str
    lon: float
    lat: float
    observed_at_utc: str


@dataclass(frozen=True)
class CompartmentGeometry:
    seq: int
    code: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class Attribution:
    status: str
    reason_kind: str
    reason: str
    codes: tuple[str, ...]


def _format_codes(codes: Sequence[str]) -> str:
    if len(codes) == 1:
        return f"林班 {codes[0]}"
    if len(codes) == 2:
        return f"林班 {codes[0]} 与 {codes[1]}"
    return "林班 " + "、".join(codes)


class Attributor:
    """Reusable spatial index over one immutable map version."""

    def __init__(self, compartments: Sequence[CompartmentGeometry]):
        self._compartments = list(compartments)
        self._geoms = [item.geometry for item in self._compartments]
        self._codes = [item.code for item in self._compartments]
        self._tree = STRtree(self._geoms) if self._geoms else None
        self._prepared = [prepared.prep(geom) for geom in self._geoms]

    def attribute_one(self, hotspot: HotspotPoint) -> Attribution:
        if not self._geoms:
            return Attribution(UNMATCHED, OUTSIDE, "当前林班底图没有可用要素", ())

        point = Point(hotspot.lon, hotspot.lat)
        # tuple of positional indices returned by STRtree.query
        assert self._tree is not None
        hits: list[tuple[str, bool]] = []  # (code, point lies only on boundary)
        for raw_idx in self._tree.query(point):
            idx = int(raw_idx)
            geom = self._geoms[idx]
            if not geom.covers(point):
                continue
            hits.append((self._codes[idx], not self._prepared[idx].contains(point)))

        if not hits:
            return Attribution(UNMATCHED, OUTSIDE, "热点位于全部林班范围之外", ())

        hits.sort(key=lambda item: item[0])
        codes = tuple(code for code, _ in hits)
        if len(hits) == 1:
            code, boundary_only = hits[0]
            if boundary_only:
                return Attribution(
                    MATCHED, BOUNDARY, f"热点位于林班 {code} 边界上（按含边界规则赋码）", codes
                )
            return Attribution(MATCHED, INTERIOR, f"热点仅落入林班 {code}", codes)

        if all(boundary_only for _, boundary_only in hits):
            return Attribution(
                CONFLICT,
                SHARED_BOUNDARY,
                f"共边：热点位于{_format_codes(codes)} 的公共边界上，无法唯一归属",
                codes,
            )
        return Attribution(
            CONFLICT,
            OVERLAP,
            f"重叠区：热点同时落入{_format_codes(codes)}，无法唯一归属",
            codes,
        )

    def attribute(self, hotspots: Iterable[HotspotPoint]) -> list[Attribution]:
        return [self.attribute_one(hotspot) for hotspot in hotspots]


def attribute_hotspots(
    hotspots: Iterable[HotspotPoint],
    compartments: Sequence[CompartmentGeometry],
) -> list[Attribution]:
    """Convenience wrapper returning one attribution per hotspot, in order."""

    return Attributor(compartments).attribute(hotspots)
