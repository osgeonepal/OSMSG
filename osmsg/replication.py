"""OSM replication URL helpers, planet changefiles + planet changesets."""

from __future__ import annotations

from datetime import UTC, datetime

from osmium.replication.server import ReplicationServer

from ._http import session
from .exceptions import OsmsgError

PLANET_BASE = "https://planet.openstreetmap.org/replication"
SHORTCUTS = {
    "minute": f"{PLANET_BASE}/minute",
    "hour": f"{PLANET_BASE}/hour",
    "day": f"{PLANET_BASE}/day",
}
CHANGESETS_REPLICATION = f"{PLANET_BASE}/changesets/"


def resolve_url(value: str) -> str:
    """`minute|hour|day` → planet URL, else passthrough (after stripping trailing /)."""
    if value in SHORTCUTS:
        return SHORTCUTS[value]
    return value.rstrip("/")


def seq_to_timestamp(state_url: str) -> datetime:
    """Parse a replication state file and return its timestamp (UTC)."""
    txt = session.get(state_url).text
    start = txt.find("timestamp=") + len("timestamp=")
    end = txt.find("\n", start)
    raw = txt[start:end].replace("\\", "")
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def changefile_seq_timestamp(base_url: str, seq: int) -> datetime:
    """Timestamp of a changefile-replication seq (the diff's `state.txt` `timestamp=` line)."""
    return seq_to_timestamp(ReplicationServer(base_url).get_state_url(seq))


def changefile_download_urls(
    start_date: datetime | None,
    end_date: datetime,
    base_url: str,
    *,
    resume_seq: int | None = None,
    cs_ts: datetime | None = None,
) -> tuple[list[str], datetime, int, int, str, str]:
    """Resolve [start_seq, last_seq] for the time range, plus the URL list to fetch.

    resume_seq, when given, is used verbatim (no backward pad). cs_ts gates the
    upper bound on resume ticks: last_seq stays behind cs_ts so every (seq, cs) row
    written has its parent changeset in place.
    """
    repl = ReplicationServer(base_url)

    if resume_seq is not None:
        seq = resume_seq
    else:
        if start_date is None:
            raise OsmsgError("changefile_download_urls requires either start_date or resume_seq")
        seq = repl.timestamp_to_sequence(start_date)
        if seq is None:
            raise OsmsgError(f"Cannot reach replication service '{base_url}'")

        start_seq_time = seq_to_timestamp(repl.get_state_url(seq))
        if start_date > start_seq_time:
            # Pad backwards by one window so we never miss a diff straddling the boundary.
            if "minute" in base_url:
                seq = (seq + int((start_date - start_seq_time).total_seconds() / 60)) - 60
            elif "hour" in base_url:
                seq = (seq + int((start_date - start_seq_time).total_seconds() / 3600)) - 1

    start_seq = seq
    start_seq_url = repl.get_state_url(start_seq)

    state = repl.get_state_info()
    if state is None:
        raise OsmsgError(f"Could not fetch state info from {base_url}")
    server_seq, server_ts = state
    server_ts = server_ts.astimezone(UTC)

    last_seq = server_seq
    if end_date:
        end_seq = repl.timestamp_to_sequence(end_date)
        if end_seq is None:
            raise OsmsgError(f"Could not resolve end_date {end_date}")
        # +1 only when end_seq's state_ts is strictly before end_date, since that is the
        # only diff that can contain edits in (state_ts(end_seq), end_date].
        end_seq_ts = seq_to_timestamp(repl.get_state_url(end_seq))
        if end_seq_ts < end_date:
            end_seq += 1
        last_seq = min(end_seq, server_seq)

    # Hold cf one diff behind cs when cs is the slower stream, so every (seq, cs) row
    # written has a parent in `changesets` already.
    if resume_seq is not None and cs_ts is not None:
        target_ts = end_date if end_date else server_ts
        if target_ts > cs_ts:
            last_seq -= 1

    if seq >= last_seq:
        return [], server_ts, start_seq, last_seq, start_seq_url, repl.get_state_url(last_seq)

    end_seq_url = repl.get_state_url(last_seq)
    urls = []
    is_geofabrik = "geofabrik" in base_url
    while seq <= last_seq:
        diff_url = repl.get_diff_url(seq)
        if is_geofabrik:
            diff_url = diff_url.replace("download.geofabrik", "osm-internal.download.geofabrik")
        urls.append(diff_url)
        seq += 1
    return urls, server_ts, start_seq, last_seq, start_seq_url, end_seq_url


class ChangesetReplication:
    """Planet changeset replication URL helper."""

    # OSM caps changeset open time at 24h, so 24 is the maximum useful pad. Default 1h
    # keeps first-run bootstraps cheap; see README "Configuration" for when to raise it.
    DEFAULT_PAD_HOURS = 1

    def __init__(self, base_url: str = CHANGESETS_REPLICATION, *, pad_hours: int = DEFAULT_PAD_HOURS) -> None:
        self.base = base_url
        self.pad_min = pad_hours * 60

    def _state(self) -> tuple[int, datetime]:
        txt = session.get(self.base + "state.yaml").text
        seq = int(txt.split("sequence: ")[1])
        last_run = datetime.strptime(txt.split("last_run: ")[1][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return seq, last_run

    def _padded(self, seq: int) -> str:
        s = str(seq).zfill(9)
        return f"{s[:3]}/{s[3:6]}/{s[6:]}"

    def diff_url(self, seq: int) -> str:
        return f"{self.base}{self._padded(seq)}.osm.gz"

    def state_url(self, seq: int) -> str:
        return f"{self.base}{self._padded(seq)}.state.txt"

    def timestamp_to_sequence(self, ts: datetime) -> int:
        cur_seq, last_run = self._state()
        wanted = int((ts - last_run).total_seconds() / 60) + cur_seq
        return max(1, min(wanted, cur_seq))

    def sequence_to_timestamp(self, seq: int) -> datetime:
        txt = session.get(self.state_url(seq)).text
        return datetime.strptime(txt.split("last_run: ")[1][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    def download_urls(
        self,
        start_date: datetime,
        end_date: datetime | None = None,
        *,
        resume_seq: int | None = None,
    ) -> tuple[list[str], int, int]:
        """Resolve [start_seq, end_seq] for the requested window.

        When ``resume_seq`` is provided (the --update fast path), we trust prior state:
        every changeset whose minute-diff sequence is < resume_seq has already been
        captured in the changesets table, so we skip the backward pad entirely.
        """
        if resume_seq is not None:
            start_seq = resume_seq
        else:
            start_seq = self.timestamp_to_sequence(start_date)
            start_ts = self.sequence_to_timestamp(start_seq)
            if start_ts > start_date:
                start_seq -= int((start_ts - start_date).total_seconds() / 60)
                start_ts = self.sequence_to_timestamp(start_seq)
            if start_date > start_ts and (start_date - start_ts).seconds != 15 * 60:
                start_seq += int((start_date - start_ts).total_seconds() / 60)
            start_seq -= self.pad_min

        cur_seq, last_run = self._state()
        if end_date is None or end_date > last_run:
            end_seq = cur_seq
        else:
            end_seq = self.timestamp_to_sequence(end_date)
            end_ts = self.sequence_to_timestamp(end_seq)
            if end_date > end_ts:
                # Step to the diff covering end_date, plus one so edits at end_date land.
                end_seq += int((end_date - end_ts).total_seconds() / 60) + 1
            end_seq = min(end_seq, cur_seq)

        if start_seq >= end_seq:
            return [], start_seq, end_seq

        urls = [self.diff_url(s) for s in range(start_seq, end_seq + 1)]
        return urls, start_seq, end_seq
