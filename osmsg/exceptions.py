"""Typed exceptions for library callers.

The CLI layer catches these and translates to exit codes; library users catch
them where they integrate the pipeline.
"""

from __future__ import annotations


class OsmsgError(Exception):
    """Base for every error osmsg raises at API boundaries."""


class UnknownRegionError(OsmsgError):
    """A region id was not present in the live Geofabrik index."""


class CredentialsRequiredError(OsmsgError):
    """A geofabrik URL was requested but no OSM credentials are available
    (no flag, no env var, no interactive TTY)."""


class GeofabrikAuthError(OsmsgError):
    """The OAuth 2.0 cookie handshake against OSM/Geofabrik failed."""


class NoDataFoundError(Exception):
    """Empty range, info condition, not a failure (CLI exits 0). Not an OsmsgError on purpose."""


__all__ = [
    "CredentialsRequiredError",
    "GeofabrikAuthError",
    "NoDataFoundError",
    "OsmsgError",
    "UnknownRegionError",
]
