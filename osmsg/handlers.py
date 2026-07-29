"""osmium SimpleHandler subclasses driving the in-memory accumulators."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import osmium
import osmium.geom
from shapely import wkt as shapely_wkt
from shapely.geometry import box

from .models import Action, Changeset, ChangesetStats, TagValueStat, User
from .stats import MAX_WAY_LENGTH_M

HASHTAG_RE = re.compile(r"#[\w-]+")


class ChangesetHandler(osmium.SimpleHandler):
    """Reads changeset replication files; emits hashtags + bbox per matched changeset."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.config = config
        self.users: dict[int, User] = {}
        self.changesets: dict[int, Changeset] = {}
        wkt = config.get("geom_filter_wkt")
        self._geom = shapely_wkt.loads(wkt) if wkt else None

    def changeset(self, c) -> None:
        if c.id in self.changesets:
            return
        cfg = self.config

        # Drop closed-before-window changesets so attach_metadata can't leak their
        # hashtags onto in-window users.
        start = cfg.get("window_start_utc")
        if start is not None and not c.open:
            closed = c.closed_at
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=dt.UTC)
            if closed <= start:
                return

        if self._geom is not None:
            if not c.bounds.valid():
                return
            bbox = box(
                c.bounds.bottom_left.lon,
                c.bounds.bottom_left.lat,
                c.bounds.top_right.lon,
                c.bounds.top_right.lat,
            )
            if not self._geom.intersects(bbox):
                return

        keep = bool(cfg["changeset_meta"] and not cfg["hashtags"])
        # Some editors only fill the `hashtags` tag (comment stays generic); checking
        # comment alone silently drops those. Tokenize via regex on both, real data
        # mixes `;`, space, and comma as separators inside `hashtags`.
        comment = c.tags.get("comment", "")
        hashtags_field = c.tags.get("hashtags", "")
        inline_tokens = HASHTAG_RE.findall(comment)
        field_tokens = HASHTAG_RE.findall(hashtags_field)
        if cfg["hashtags"]:
            if cfg["exact_lookup"]:
                found = {h.lower() for h in inline_tokens}
                found.update(h.lower() for h in field_tokens)
                keep = any(h.lower() in found for h in cfg["hashtags"])
            else:
                haystack = (comment + "\n" + hashtags_field).lower()
                keep = any(h.lower() in haystack for h in cfg["hashtags"])

        if keep and cfg["whitelisted_users"]:
            keep = c.user in cfg["whitelisted_users"]

        if not keep:
            return

        hashtags_list: list[str] = []
        seen: set[str] = set()
        for tok in inline_tokens + field_tokens:
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                hashtags_list.append(tok)
        bbox = None
        if c.bounds.valid():
            b = c.bounds
            bbox = (b.bottom_left.lon, b.bottom_left.lat, b.top_right.lon, b.top_right.lat)

        created_at = c.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.UTC)

        self.users[c.uid] = User(uid=c.uid, username=c.user)
        self.changesets[c.id] = Changeset(
            changeset_id=c.id,
            uid=c.uid,
            created_at=created_at,
            hashtags=hashtags_list,
            editor=c.tags.get("created_by"),
            bbox=bbox,
        )


class ChangefileHandler(osmium.SimpleHandler):
    """Reads OSC changefiles; accumulates per-changeset element + tag counters."""

    def __init__(self, config: dict[str, Any], sequence_id: int, valid_changesets: set[int] | None = None) -> None:
        super().__init__()
        self.config = config
        self.start = config["start_date_utc"]
        self.seq_id = sequence_id
        # None == no filter; empty set == filter matched nothing (collect nothing).
        self.valid_changesets = valid_changesets

        self.users: dict[int, User] = {}
        self.stubs: dict[int, Changeset] = {}
        self.stats: dict[int, ChangesetStats] = {}

    def _should_collect(self, uname: str, cs_id: int) -> bool:
        if self.valid_changesets is not None and cs_id not in self.valid_changesets:
            return False
        whitelist = self.config["whitelisted_users"]
        return not (whitelist and uname not in whitelist)

    def _record(self, uid: int, uname: str, cs_id: int) -> None:
        if uid not in self.users:
            self.users[uid] = User(uid=uid, username=uname)
        if cs_id not in self.stubs:
            self.stubs[cs_id] = Changeset(changeset_id=cs_id, uid=uid)

    @staticmethod
    def _way_length(way_nodes) -> float:
        """Haversine metres of an OPEN way (first ref != last ref); 0 for a closed way (an area, not a
        length) or when geometry is unavailable. In the live per-diff path a node from an earlier diff
        has no location here (InvalidLocationError) -> 0; the backfill's global index measures those."""
        if not way_nodes or len(way_nodes) < 2 or way_nodes[0].ref == way_nodes[-1].ref:
            return 0.0
        try:
            length = osmium.geom.haversine_distance(way_nodes)
        except osmium.InvalidLocationError:
            return 0.0
        return length if length <= MAX_WAY_LENGTH_M else 0.0

    def _accumulate(self, uid, uname, cs_id, version, tags, kind, way_nodes=None) -> None:
        action = Action.DELETE if version == 0 else Action.CREATE if version == 1 else Action.MODIFY

        self._record(uid, uname, cs_id)
        stats = self.stats.setdefault(cs_id, ChangesetStats(changeset_id=cs_id, uid=uid, seq_id=self.seq_id))

        if kind == "nodes":
            stats.nodes.add(action)
            if tags and action is not Action.DELETE:
                if action is Action.CREATE:
                    stats.poi_created += 1
                elif action is Action.MODIFY:
                    stats.poi_modified += 1
        elif kind == "ways":
            stats.ways.add(action)
        elif kind == "relations":
            stats.rels.add(action)

        if not tags or action is Action.DELETE:
            return

        # Way length attaches to every tag of an open way on create; closed ways are areas, not lengths.
        length_m = self._way_length(way_nodes) if action is Action.CREATE else 0.0
        cfg = self.config

        if cfg["tag_mode"] != "none":
            for k, v in tags:
                tv = stats.tag_stats.setdefault(k, {}).setdefault(v, TagValueStat())
                tv.add(action)
                if length_m:
                    tv.add_length(length_m)
        elif cfg["additional_tags"]:
            for k in cfg["additional_tags"]:
                if k not in tags:
                    continue
                v = tags[k]
                tv = stats.tag_stats.setdefault(k, {}).setdefault(v, TagValueStat())
                tv.add(action)
                if length_m:
                    tv.add_length(length_m)

    def _in_window(self, ts) -> bool:
        # Lower-bound only; disjoint coverage between ticks comes from the seq boundary
        # (next tick resumes at last_seq+1, with state.last_ts = state_ts(last_seq)).
        return ts >= self.start

    def node(self, n) -> None:
        if not self._in_window(n.timestamp):
            return
        if not self._should_collect(n.user, n.changeset):
            return
        self._accumulate(n.uid, n.user, n.changeset, 0 if n.deleted else n.version, n.tags, "nodes")

    def way(self, w) -> None:
        if not self._in_window(w.timestamp):
            return
        if not self._should_collect(w.user, w.changeset):
            return
        self._accumulate(w.uid, w.user, w.changeset, 0 if w.deleted else w.version, w.tags, "ways", w.nodes)

    def relation(self, r) -> None:
        if not self._in_window(r.timestamp):
            return
        if not self._should_collect(r.user, r.changeset):
            return
        self._accumulate(r.uid, r.user, r.changeset, 0 if r.deleted else r.version, r.tags, "relations")
