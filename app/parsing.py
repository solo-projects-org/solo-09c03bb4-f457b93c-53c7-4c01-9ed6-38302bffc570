"""Input parsing/validation for the two uploads.

Both parsers are strict about the things that would silently corrupt the
warehouse output (bad coordinates, duplicate codes, unknown columns) and
lenient about presentation (BOM, column order, extra columns, casing).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shapely import wkb
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_GEOJSON_BYTES = 128 * 1024 * 1024
MAX_ROWS = 500_000
MAX_FEATURES = 200_000

HOTSPOT_ALIASES = {
    "hotspot_no": {"hotspot_no", "热点号", "热点编号", "hotspot", "hotspot_id", "热点id"},
    "lon": {"lon", "lng", "longitude", "经度", "x"},
    "lat": {"lat", "latitude", "纬度", "y"},
    "observed_at": {"observed_at", "utc", "time", "timestamp", "datetime", "时刻", "时间", "utc时刻", "观测时间"},
}
CODE_ALIASES = {"code", "林班号", "林班", "compartment", "compartment_code", "compartment_no", "林班编号"}


class InputError(ValueError):
    """A user-fixable problem with an uploaded file."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm_header(name: str) -> str:
    return re.sub(r"[\s_\-]+", "_", name.strip().lower())


def _decode(data: bytes, what: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InputError(f"{what} 不是有效的 UTF-8/GB18030 文本")


# --------------------------------------------------------------------------
# Hotspot CSV
# --------------------------------------------------------------------------


@dataclass
class Hotspot:
    seq: int
    source_row: int
    hotspot_no: str
    lon: float
    lat: float
    observed_at_utc: str
    raw: dict[str, str] = field(default_factory=dict)


_TS_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y%m%d%H%M%S",
    "%Y-%m-%d",
)


def parse_utc(value: str) -> str:
    """Normalise a UTC timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    Explicit offsets are converted and a trailing ``Z`` is accepted.  A naive
    value is interpreted as UTC because the column is defined as UTC — this is
    a deliberate, documented assumption, not silent localisation.
    """

    text = value.strip()
    if not text:
        raise InputError("时刻为空")
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        dt = None
    if dt is None:
        for pattern in _TS_PATTERNS:
            try:
                dt = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if dt is None:
        raise InputError(f"无法识别的时刻 {text!r}（应为 UTC 时刻）")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _map_columns(headers: list[str], aliases: dict[str, set[str]], required: list[str]) -> dict[str, str]:
    normalised = {_norm_header(h): h for h in headers if h.strip()}
    mapping: dict[str, str] = {}
    for canonical in required:
        for header_norm, original in normalised.items():
            if header_norm in aliases[canonical]:
                mapping[canonical] = original
                break
        else:
            raise InputError(
                f"CSV 缺少必需列 {canonical}（可接受表头：{', '.join(sorted(aliases[canonical]))}）"
            )
    return mapping


def _parse_coord(value: str, what: str, lo: float, hi: float) -> float:
    text = value.strip()
    if not text:
        raise InputError(f"{what}为空")
    try:
        number = float(text)
    except ValueError:
        raise InputError(f"{what} {text!r} 不是数字") from None
    if not (lo <= number <= hi):
        raise InputError(f"{what} {number} 超出范围 [{lo}, {hi}]")
    return number


def parse_hotspots_csv(data: bytes, filename: str) -> list[Hotspot]:
    if not data:
        raise InputError("热点 CSV 为空")
    if len(data) > MAX_CSV_BYTES:
        raise InputError(f"热点 CSV 超过 {MAX_CSV_BYTES // 1024 // 1024} MB 限制")
    text = _decode(data, "热点 CSV")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise InputError("热点 CSV 缺少表头")
    mapping = _map_columns(list(reader.fieldnames), HOTSPOT_ALIASES, ["hotspot_no", "lon", "lat", "observed_at"])
    rows: list[Hotspot] = []
    for source_row, record in enumerate(reader, start=2):
        if None in record or (record.get(None) or "").strip():
            raise InputError(f"第 {source_row} 行列数多于表头，请检查是否含未转义的逗号")
        values = {key: (record.get(col) or "").strip() for key, col in mapping.items()}
        if not any(values.values()):
            continue  # blank line
        if len(rows) >= MAX_ROWS:
            raise InputError(f"热点数量超过 {MAX_ROWS} 行限制")
        hotspot_no = values["hotspot_no"]
        if not hotspot_no:
            raise InputError(f"第 {source_row} 行热点号为空")
        lon = _parse_coord(values["lon"], f"第 {source_row} 行经度", -180.0, 180.0)
        lat = _parse_coord(values["lat"], f"第 {source_row} 行纬度", -90.0, 90.0)
        try:
            observed = parse_utc(values["observed_at"])
        except InputError as exc:
            raise InputError(f"第 {source_row} 行：{exc}") from None
        raw = {str(k): str(v) for k, v in record.items() if k is not None}
        rows.append(
            Hotspot(
                seq=len(rows),
                source_row=source_row,
                hotspot_no=hotspot_no,
                lon=lon,
                lat=lat,
                observed_at_utc=observed,
                raw=raw,
            )
        )
    if not rows:
        raise InputError("热点 CSV 没有数据行")
    return rows


# --------------------------------------------------------------------------
# Compartment GeoJSON
# --------------------------------------------------------------------------


@dataclass
class Compartment:
    seq: int
    code: str
    geometry: BaseGeometry
    minx: float
    miny: float
    maxx: float
    maxy: float


def _feature_code(props: dict[str, Any], feature_index: int) -> str:
    for key, value in props.items():
        if _norm_header(str(key)) in CODE_ALIASES:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
    raise InputError(f"第 {feature_index + 1} 个要素缺少林班号属性（可接受键：{', '.join(sorted(CODE_ALIASES))}）")


def _coerce_geometry(geom: BaseGeometry) -> tuple[BaseGeometry, bool]:
    if geom.is_empty:
        raise InputError("几何为空")
    if not geom.is_valid:
        geom = make_valid(geom)
        if geom.is_empty:
            raise InputError("几何无效且无法修复")
        return geom, True
    return geom, False


def _allowed(geom: BaseGeometry) -> bool:
    types = {"Polygon", "MultiPolygon", "GeometryCollection"}
    if geom.geom_type not in types:
        return False
    if geom.geom_type == "GeometryCollection":
        return all(_allowed(g) for g in geom.geoms)
    return True


def parse_compartment_geojson(data: bytes, filename: str) -> tuple[list[Compartment], int]:
    if not data:
        raise InputError("林班 GeoJSON 为空")
    if len(data) > MAX_GEOJSON_BYTES:
        raise InputError(f"林班 GeoJSON 超过 {MAX_GEOJSON_BYTES // 1024 // 1024} MB 限制")
    try:
        doc = json.loads(_decode(data, "林班 GeoJSON"))
    except json.JSONDecodeError as exc:
        raise InputError(f"林班 GeoJSON 解析失败：{exc.msg}（第 {exc.lineno} 行）") from None
    if not isinstance(doc, dict):
        raise InputError("林班 GeoJSON 顶层必须是对象")
    if doc.get("type") == "FeatureCollection":
        features = doc.get("features")
        if not isinstance(features, list):
            raise InputError("FeatureCollection.features 必须是数组")
    elif doc.get("type") == "Feature":
        features = [doc]
    else:
        raise InputError("林班 GeoJSON 必须是 Feature 或 FeatureCollection")

    compartments: list[Compartment] = []
    repaired = 0
    seen: dict[str, int] = {}
    for index, feature in enumerate(features):
        if len(compartments) >= MAX_FEATURES:
            raise InputError(f"林班要素超过 {MAX_FEATURES} 个限制")
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise InputError(f"第 {index + 1} 个要素不是 Feature 对象")
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            raise InputError(f"第 {index + 1} 个要素的 properties 必须是对象")
        code = _feature_code(props, index)
        if code in seen:
            raise InputError(f"林班号 {code!r} 重复出现（第 {seen[code] + 1} 与第 {index + 1} 个要素）")
        geometry_doc = feature.get("geometry")
        if not isinstance(geometry_doc, dict):
            raise InputError(f"林班 {code!r} 缺少几何")
        try:
            geom = shape(geometry_doc)
        except Exception as exc:  # shapely raises a variety of types here
            raise InputError(f"林班 {code!r} 几何无法解析：{exc}") from None
        if not _allowed(geom):
            raise InputError(f"林班 {code!r} 几何类型 {geom.geom_type} 不受支持（需为面）")
        try:
            geom, was_repaired = _coerce_geometry(geom)
        except InputError as exc:
            raise InputError(f"林班 {code!r}：{exc}") from None
        repaired += int(was_repaired)
        bounds = geom.bounds
        if len(bounds) != 4 or any(value != value for value in bounds):  # NaN guard
            raise InputError(f"林班 {code!r} 几何坐标无效")
        minx, miny, maxx, maxy = bounds
        if not (-180.0 <= minx <= 180.0 and -180.0 <= maxx <= 180.0 and -90.0 <= miny <= 90.0 and -90.0 <= maxy <= 90.0):
            raise InputError(f"林班 {code!r} 坐标超出经纬度范围")
        seen[code] = index
        compartments.append(
            Compartment(
                seq=len(compartments),
                code=code,
                geometry=geom,
                minx=minx,
                miny=miny,
                maxx=maxx,
                maxy=maxy,
            )
        )
    if not compartments:
        raise InputError("林班 GeoJSON 没有要素")
    return compartments, repaired


def geometry_to_wkb(geom: BaseGeometry) -> bytes:
    return wkb.dumps(geom, hex=False, output_dimension=2)


def wkb_to_geometry(blob: bytes) -> BaseGeometry:
    return wkb.loads(blob)
