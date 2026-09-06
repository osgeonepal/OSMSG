import io
import json

from osmsg.maintain import check


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return io.BytesIO(self._data)

    def __exit__(self, *a):
        return False


def test_fetch_closed_skips_open_and_extracts_metadata(monkeypatch):
    payload = {
        "changesets": [
            {"id": 1, "open": True, "created_at": "2026-08-25T10:00:00Z", "tags": {"comment": "#foo"}},
            {
                "id": 2,
                "open": False,
                "created_at": "2026-08-23T13:30:33Z",
                "tags": {"comment": "adding #hotosm-project-42 buildings", "created_by": "iD 2.42"},
                "min_lon": 1.0,
                "min_lat": 2.0,
                "max_lon": 3.0,
                "max_lat": 4.0,
            },
        ]
    }
    monkeypatch.setattr(check, "urlopen", lambda url, timeout=0: _FakeResp(payload))
    out = check.fetch_closed([1, 2])
    assert 1 not in out  # still open -> skipped
    assert out[2]["created_at"] == "2026-08-23T13:30:33Z"
    assert out[2]["editor"] == "iD 2.42"
    assert out[2]["hashtags"] == ["#hotosm-project-42"]
    assert out[2]["min_lon"] == 1.0


def test_sql_literal_helpers_escape():
    assert check.sql_literal(None) == "NULL"
    assert check.sql_literal("a'b") == "'a''b'"
    assert check._sql_number(None) == "NULL"
    assert check._sql_number(1.5) == "1.5"
    assert check._sql_array([]) == "ARRAY[]::text[]"
    assert check._sql_array(["#a", "#b"]) == "ARRAY['#a','#b']::text[]"
