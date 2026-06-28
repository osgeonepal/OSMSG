"""Split an OSM PBF into N parts at blob boundaries without decoding object data, so parallel readers
can each stream one part. Every object lands in exactly one part."""

import contextlib
import pathlib
import struct


def read_blob(handle) -> tuple[bytes, bytes, str] | None:
    """Return (header_len_prefix + header_bytes, blob_bytes, type) or None at EOF."""
    raw_len = handle.read(4)
    if len(raw_len) < 4:
        return None
    (header_len,) = struct.unpack(">I", raw_len)
    header = handle.read(header_len)
    blob_type, datasize = _parse_blobheader(header)
    blob = handle.read(datasize)
    return raw_len + header, blob, blob_type


def _parse_blobheader(buf: bytes) -> tuple[str, int]:
    blob_type, datasize, i = "", 0, 0
    while i < len(buf):
        key = buf[i]
        i += 1
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, i = _uvarint(buf, i)
            data = buf[i : i + length]
            i += length
            if field == 1:
                blob_type = data.decode()
        elif wire == 0:
            val, i = _uvarint(buf, i)
            if field == 3:
                datasize = val
        else:
            raise ValueError(f"unexpected wire type {wire}")
    return blob_type, datasize


def _uvarint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def split_pbf(src: str, out_dir: pathlib.Path, n: int) -> list[pathlib.Path]:
    """Round-robin every OSMData blob into N part files, each prefixed with the source OSMHeader."""
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [out_dir / f"part{k:03d}.osm.pbf" for k in range(n)]
    with contextlib.ExitStack() as stack:
        f = stack.enter_context(open(src, "rb"))
        first = read_blob(f)
        if first is None or first[2] != "OSMHeader":
            raise ValueError("first blob is not OSMHeader")
        header_prefix, header_blob, _ = first
        outs = [stack.enter_context(open(p, "wb")) for p in parts]
        for o in outs:
            o.write(header_prefix)
            o.write(header_blob)
        idx = 0
        while True:
            blob = read_blob(f)
            if blob is None:
                break
            prefix, body, btype = blob
            if btype != "OSMData":
                continue
            outs[idx].write(prefix)
            outs[idx].write(body)
            idx = (idx + 1) % n
    return parts
