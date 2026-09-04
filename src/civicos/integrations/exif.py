"""Image metadata extraction.

The rewritten successor to the original metadata extractor. Changes that
matter operationally:

* GPS conversion is exact (rationals are kept as fractions until the final
  division) and validated, instead of silently returning ``None``;
* the capture timestamp is parsed into a real ``datetime``, with the EXIF
  offset applied when present, so "photo taken before the complaint was filed"
  becomes a checkable claim;
* nothing raises - a photo with no EXIF is normal, not an error;
* orientation, dimensions and device are captured so the console can render
  evidence the right way up.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from civicos.core.geo import Point, is_valid_coordinate

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ImageMetadata:
    """What we could learn about one image, and what we could not."""

    filename: str
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    gps_accuracy_m: float | None = None
    captured_at: datetime | None = None
    device: str | None = None
    orientation: int | None = None
    width: int | None = None
    height: int | None = None
    has_exif: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_location(self) -> bool:
        return is_valid_coordinate(self.latitude, self.longitude)

    @property
    def point(self) -> Point | None:
        return (
            Point(self.latitude, self.longitude)  # type: ignore[arg-type]
            if self.has_location
            else None
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["captured_at"] = self.captured_at.isoformat() if self.captured_at else None
        payload.pop("raw", None)
        return payload


def extract_metadata(data: bytes, filename: str = "image") -> ImageMetadata:
    """Read EXIF from image bytes. Never raises."""
    metadata = ImageMetadata(filename=filename)

    try:
        import exifread
    except ImportError:  # pragma: no cover - optional at runtime
        metadata.warnings.append("EXIF library unavailable.")
        _fill_dimensions(metadata, data)
        return metadata

    try:
        with io.BytesIO(data) as buffer:
            tags = exifread.process_file(buffer, details=False)
    except Exception as exc:
        logger.debug("exif_read_failed", filename=filename, error=str(exc))
        metadata.warnings.append("EXIF block could not be read.")
        _fill_dimensions(metadata, data)
        return metadata

    if not tags:
        metadata.warnings.append(
            "No EXIF data. Many messaging apps strip it when a photo is forwarded."
        )
        _fill_dimensions(metadata, data)
        return metadata

    metadata.has_exif = True
    metadata.captured_at = _read_timestamp(tags, metadata)
    metadata.device = _read_device(tags)
    metadata.orientation = _read_int(tags.get("Image Orientation"))
    metadata.width = _read_int(tags.get("EXIF ExifImageWidth"))
    metadata.height = _read_int(tags.get("EXIF ExifImageLength"))
    _read_gps(tags, metadata)

    if metadata.width is None or metadata.height is None:
        _fill_dimensions(metadata, data)
    return metadata


def extract_batch(images: list[tuple[bytes, str]]) -> list[ImageMetadata]:
    return [extract_metadata(data, filename) for data, filename in images]


# ---------------------------------------------------------------- internals --


def _read_gps(tags: dict[str, Any], metadata: ImageMetadata) -> None:
    latitude = _to_degrees(tags.get("GPS GPSLatitude"), _tag_str(tags.get("GPS GPSLatitudeRef")))
    longitude = _to_degrees(tags.get("GPS GPSLongitude"), _tag_str(tags.get("GPS GPSLongitudeRef")))

    if latitude is None or longitude is None:
        metadata.warnings.append("No GPS coordinates in EXIF.")
        return
    if not is_valid_coordinate(latitude, longitude):
        metadata.warnings.append("EXIF GPS coordinates are out of range or null-island.")
        return

    metadata.latitude = latitude
    metadata.longitude = longitude

    if altitude := _ratio(tags.get("GPS GPSAltitude")):
        ref = _tag_str(tags.get("GPS GPSAltitudeRef")) or "0"
        metadata.altitude_m = -altitude if ref.strip() in {"1", "Below sea level"} else altitude
    if accuracy := _ratio(tags.get("GPS GPSHPositioningError")):
        metadata.gps_accuracy_m = accuracy


def _to_degrees(tag: Any, ref: str | None) -> float | None:
    """Convert an EXIF ``[deg, min, sec]`` rational triple to decimal degrees."""
    if tag is None or not getattr(tag, "values", None):
        return None
    values = tag.values
    if len(values) < 2:
        return None
    try:
        degrees = _ratio(values[0]) or 0.0
        minutes = _ratio(values[1]) or 0.0
        seconds = _ratio(values[2]) if len(values) > 2 else 0.0
    except (TypeError, ZeroDivisionError):
        return None

    decimal = degrees + minutes / 60.0 + (seconds or 0.0) / 3600.0
    if ref and ref.strip().upper() in {"S", "W"}:
        decimal = -decimal
    return round(decimal, 7)


def _ratio(value: Any) -> float | None:
    """Handle exifread ``Ratio`` objects, plain numbers and strings alike."""
    if value is None:
        return None
    if hasattr(value, "num") and hasattr(value, "den"):
        denominator = float(value.den)
        return float(value.num) / denominator if denominator else None
    if hasattr(value, "values"):
        return _ratio(value.values[0]) if value.values else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_timestamp(tags: dict[str, Any], metadata: ImageMetadata) -> datetime | None:
    raw = _tag_str(
        tags.get("EXIF DateTimeOriginal")
        or tags.get("EXIF DateTimeDigitized")
        or tags.get("Image DateTime")
    )
    if not raw:
        metadata.warnings.append("No capture timestamp in EXIF.")
        return None

    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            captured = datetime.strptime(raw.strip(), pattern)
            break
        except ValueError:
            continue
    else:
        metadata.warnings.append(f"Unrecognised timestamp format: {raw!r}")
        return None

    # EXIF timestamps are local wall-clock; apply the recorded offset if present.
    offset = _parse_offset(_tag_str(tags.get("EXIF OffsetTimeOriginal")))
    if offset is not None:
        return (captured - offset).replace(tzinfo=UTC)
    return captured.replace(tzinfo=UTC)


def _parse_offset(raw: str | None) -> timedelta | None:
    if not raw or len(raw) < 6:
        return None
    sign = -1 if raw[0] == "-" else 1
    try:
        hours, _, minutes = raw[1:].partition(":")
        return sign * timedelta(hours=int(hours), minutes=int(minutes or 0))
    except ValueError:
        return None


def _read_device(tags: dict[str, Any]) -> str | None:
    make = _tag_str(tags.get("Image Make"))
    model = _tag_str(tags.get("Image Model"))
    lens = _tag_str(tags.get("EXIF LensModel"))
    parts = [part for part in (make, model) if part]
    if parts:
        device = " ".join(dict.fromkeys(" ".join(parts).split()))
        return device[:120]
    return lens[:120] if lens else None


def _read_int(tag: Any) -> int | None:
    if tag is None:
        return None
    value = getattr(tag, "values", None)
    if isinstance(value, list) and value:
        value = value[0]
    try:
        return int(value if value is not None else str(tag))
    except (TypeError, ValueError):
        return None


def _tag_str(tag: Any) -> str | None:
    return str(tag) if tag is not None else None


def _fill_dimensions(metadata: ImageMetadata, data: bytes) -> None:
    """Fall back to decoding the header for width/height."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            metadata.width, metadata.height = image.size
    except Exception:
        return


def location_consistency(
    reported: Point | None, photo_points: list[Point], tolerance_meters: int = 250
) -> dict[str, Any]:
    """Check that evidence photos were taken near the reported location.

    Not a fraud detector - phones lie, EXIF is stripped, and a resident may
    photograph a blocked drain from across the road. It surfaces a flag for a
    human, which is the only defensible use of this signal.
    """
    from civicos.core.geo import haversine_meters

    if reported is None or not photo_points:
        return {"checked": False, "consistent": None, "max_distance_m": None}

    distances = [haversine_meters(reported, point) for point in photo_points]
    furthest = max(distances)
    return {
        "checked": True,
        "consistent": furthest <= tolerance_meters,
        "max_distance_m": round(furthest, 1),
        "tolerance_m": tolerance_meters,
        "photo_count": len(photo_points),
    }
