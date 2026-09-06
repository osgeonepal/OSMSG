"""Package-level smoke tests."""

from osmsg.__version__ import __version__
from osmsg.exceptions import NoDataFoundError, OsmsgError


def test_version_is_populated():
    assert isinstance(__version__, str)
    assert __version__


def test_package_modules_import():
    from osmsg import (  # noqa: F401
        auth,
        boundary,
        cli,
        db,
        export,
        fetch,
        geofabrik,
        handlers,
        models,
        pipeline,
        replication,
        tm,
        ui,
        workers,
    )


def test_no_data_found_is_not_osmsg_error():
    """Catching OsmsgError must not silently swallow 'no data' (an info condition, not a failure)."""
    assert not issubclass(NoDataFoundError, OsmsgError)
    assert issubclass(NoDataFoundError, Exception)
