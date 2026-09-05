"""Live region lookup via Geofabrik index-v1.json."""

from functools import lru_cache
from typing import Any

from shapely.geometry import MultiPolygon, Polygon, shape

from ._http import session
from .exceptions import UnknownRegionError

INDEX_URL = "https://download.geofabrik.de/index-v1.json"


@lru_cache(maxsize=1)
def _raw_index() -> dict[str, dict[str, Any]]:
    r = session.get(INDEX_URL, timeout=60)
    r.raise_for_status()
    out: dict[str, dict[str, Any]] = {}
    for f in r.json().get("features", []):
        props = f.get("properties") or {}
        rid = props.get("id")
        if not rid:
            continue
        out[rid] = {
            "updates": (props.get("urls") or {}).get("updates"),
            "geometry": f.get("geometry"),
        }
    return out


def load_index() -> dict[str, str]:
    """Return `{region_id: updates_url}` parsed from the live Geofabrik index. Cached per process."""
    return {rid: entry["updates"] for rid, entry in _raw_index().items() if entry.get("updates")}


def country_update_url(region_id: str) -> str:
    """Resolve a Geofabrik region id (e.g. ``nepal``) to its `*-updates` base URL. Raises UnknownRegion if
    the id is not in the live index."""
    idx = load_index()
    key = region_id.lower()
    if key not in idx:
        raise UnknownRegionError(f"Geofabrik region '{region_id}' not found")
    return idx[key]


def country_geometry(region_id: str) -> Polygon | MultiPolygon:
    """Resolve a Geofabrik region id to its published polygon."""
    idx = _raw_index()
    key = region_id.lower()
    entry = idx.get(key)
    geom_dict = entry.get("geometry") if entry else None
    if not geom_dict or geom_dict.get("type") not in ("Polygon", "MultiPolygon"):
        raise UnknownRegionError(f"Geofabrik region '{region_id}' has no published polygon")
    geom = shape(geom_dict)
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise UnknownRegionError(f"Geofabrik region '{region_id}' geometry is {type(geom).__name__}")
    return geom


__all__ = ["INDEX_URL", "country_geometry", "country_update_url", "load_index"]
