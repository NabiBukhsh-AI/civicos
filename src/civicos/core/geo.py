"""Lightweight geospatial helpers.

CivicOS deliberately stores plain ``latitude``/``longitude`` columns plus a
geohash rather than requiring PostGIS: a town IT department can run the whole
platform on SQLite or vanilla Postgres. The helpers here give us proximity
search, clustering and hotspot binning without a spatial extension. When
PostGIS *is* available the same columns can be indexed with a generated
``geography`` column - see docs/architecture.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8
_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class Point:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"longitude out of range: {self.longitude}")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    min_latitude: float
    min_longitude: float
    max_latitude: float
    max_longitude: float

    def contains(self, point: Point) -> bool:
        return (
            self.min_latitude <= point.latitude <= self.max_latitude
            and self.min_longitude <= point.longitude <= self.max_longitude
        )


def is_valid_coordinate(latitude: float | None, longitude: float | None) -> bool:
    if latitude is None or longitude is None:
        return False
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False
    # (0, 0) is in the Gulf of Guinea - almost always a stripped-EXIF artefact.
    return not (abs(latitude) < 1e-7 and abs(longitude) < 1e-7)


def haversine_meters(a: Point, b: Point) -> float:
    """Great-circle distance between two points, in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bounding_box(center: Point, radius_meters: float) -> BoundingBox:
    """Cheap bounding box used to pre-filter rows before exact distance maths."""
    lat_delta = math.degrees(radius_meters / EARTH_RADIUS_M)
    cos_lat = max(math.cos(math.radians(center.latitude)), 1e-6)
    lon_delta = math.degrees(radius_meters / (EARTH_RADIUS_M * cos_lat))
    return BoundingBox(
        min_latitude=max(center.latitude - lat_delta, -90.0),
        min_longitude=max(center.longitude - lon_delta, -180.0),
        max_latitude=min(center.latitude + lat_delta, 90.0),
        max_longitude=min(center.longitude + lon_delta, 180.0),
    )


def encode_geohash(latitude: float, longitude: float, precision: int = 9) -> str:
    """Standard geohash. Precision 9 is ~4.8 m, 7 is ~150 m, 6 is ~1.2 km."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    geohash: list[str] = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    char_index = 0
    even = True

    while len(geohash) < precision:
        if even:
            mid = sum(lon_range) / 2
            if longitude > mid:
                char_index |= bits[bit]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = sum(lat_range) / 2
            if latitude > mid:
                char_index |= bits[bit]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            geohash.append(_GEOHASH_BASE32[char_index])
            bit = 0
            char_index = 0
    return "".join(geohash)


def grid_cell(latitude: float, longitude: float, cell_meters: int) -> tuple[int, int]:
    """Snap a point to an equal-area-ish grid cell for hotspot aggregation."""
    lat_step = math.degrees(cell_meters / EARTH_RADIUS_M)
    cos_lat = max(math.cos(math.radians(latitude)), 1e-6)
    lon_step = math.degrees(cell_meters / (EARTH_RADIUS_M * cos_lat))
    return (int(latitude // lat_step), int(longitude // lon_step))


def cell_center(cell: tuple[int, int], cell_meters: int, reference_latitude: float) -> Point:
    lat_step = math.degrees(cell_meters / EARTH_RADIUS_M)
    cos_lat = max(math.cos(math.radians(reference_latitude)), 1e-6)
    lon_step = math.degrees(cell_meters / (EARTH_RADIUS_M * cos_lat))
    return Point(
        latitude=(cell[0] + 0.5) * lat_step,
        longitude=(cell[1] + 0.5) * lon_step,
    )


def centroid(points: list[Point]) -> Point | None:
    """Spherical centroid - correct across the antimeridian, unlike a mean."""
    if not points:
        return None
    x = y = z = 0.0
    for point in points:
        lat = math.radians(point.latitude)
        lon = math.radians(point.longitude)
        x += math.cos(lat) * math.cos(lon)
        y += math.cos(lat) * math.sin(lon)
        z += math.sin(lat)
    count = len(points)
    x, y, z = x / count, y / count, z / count
    lon = math.atan2(y, x)
    hyp = math.sqrt(x * x + y * y)
    lat = math.atan2(z, hyp)
    return Point(latitude=math.degrees(lat), longitude=math.degrees(lon))


def point_in_polygon(point: Point, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting test. ``polygon`` is a list of ``(latitude, longitude)``.

    Used to place a report inside the correct ward / union council when the
    tenant has uploaded boundary geometry.
    """
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        intersects = (lon_i > point.longitude) != (lon_j > point.longitude)
        if intersects:
            lat_at_lon = (lat_j - lat_i) * (point.longitude - lon_i) / (lon_j - lon_i) + lat_i
            if point.latitude < lat_at_lon:
                inside = not inside
        j = i
    return inside


def format_coordinates(latitude: float, longitude: float, precision: int = 6) -> str:
    return f"{latitude:.{precision}f},{longitude:.{precision}f}"


def osm_link(latitude: float, longitude: float, zoom: int = 18) -> str:
    return (
        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
        f"&mlon={longitude:.6f}#map={zoom}/{latitude:.6f}/{longitude:.6f}"
    )
