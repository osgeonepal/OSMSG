"""Boundary GeoJSON parsing."""

import json
from pathlib import Path
from typing import Any

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry

from .exceptions import UnknownRegionError
from .geofabrik import country_geometry


def load_boundary(input_data: str) -> BaseGeometry:
    try:
        payload: Any = json.loads(input_data)
    except json.JSONDecodeError:
        path = Path(input_data)
        if path.is_file():
            payload = json.loads(path.read_text())
        else:
            try:
                return country_geometry(input_data)
            except UnknownRegionError:
                raise ValueError(
                    f"--boundary {input_data!r} is not valid JSON, a file path, or a known Geofabrik region name."
                ) from None

    geometry = payload.get("geometry") if "geometry" in payload else payload
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Boundary must be a Polygon or MultiPolygon GeoJSON.")
    geom = shape(geometry)
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    raise ValueError(f"Unexpected geometry type: {type(geom).__name__}")
