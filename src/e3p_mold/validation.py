"""Non-repairing artifact validation; no geometry is silently changed."""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from pathlib import Path

import numpy as np

MAX_FACES = 2_000_000
MAX_PNG_DECODED = 64 * 1024 * 1024


def confined_file(root, value, *, suffix, max_bytes):
    root = Path(root).resolve(strict=True)
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("artifact must be an absolute, regular file inside its attempt")
    if path.parent.resolve(strict=True) != root or path.suffix.lower() != suffix:
        raise ValueError("artifact path or type is outside the requested attempt")
    stat = path.stat()
    if not path.is_file() or stat.st_nlink != 1 or not 0 < stat.st_size <= max_bytes:
        raise ValueError("artifact is empty, linked, or exceeds the byte budget")
    return path


def validate_stl(path, *, max_bytes, check=None):
    """Check Blender's binary STL without filling holes or dropping components.

    Identical coordinates are welded only for analysis. Closed oriented edges,
    one face-connected component, connected vertex fans, finite coordinates,
    and positive signed volume are required. This is not a self-intersection,
    dimensional-clearance, wall-thickness or demoldability proof.
    """
    path = Path(path)
    size = path.stat().st_size
    if not 84 <= size <= max_bytes:
        raise ValueError("STL is empty or exceeds the output budget")
    with path.open("rb") as stream:
        header = stream.read(84)
        count = struct.unpack_from("<I", header, 80)[0]
        if not 0 < count <= MAX_FACES or size != 84 + 50 * count:
            raise ValueError("expected a complete binary STL with at most 2000000 faces")
        dtype = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
        records = np.fromfile(stream, dtype=dtype, count=count)
    if check:
        check()
    if len(records) != count or not np.isfinite(records["vertices"]).all() or not np.isfinite(records["normal"]).all():
        raise ValueError("STL contains nonfinite or incomplete geometry")
    vertices, inverse = np.unique(records["vertices"].reshape(-1, 3), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    xyz = vertices.astype(np.float64)
    triangles = xyz[faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    if np.any(np.all(cross == 0, axis=1)):
        raise ValueError("STL contains degenerate triangles")
    edges = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    _, edge_inverse, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
    if np.any(counts != 2):
        raise ValueError("STL is not a closed two-manifold surface")
    orientations = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    if np.any(np.bincount(edge_inverse, weights=orientations) != 0):
        raise ValueError("STL faces are not consistently oriented")
    parents = list(range(count))
    fans = list(range(count * 3))

    def find(table, value):
        while table[value] != value:
            table[value] = table[table[value]]
            value = table[value]
        return value

    def union(table, left, right):
        a, b = find(table, left), find(table, right)
        if a != b:
            table[b] = a

    # Pair the two incident faces of every edge. Separate solids touching only
    # at a vertex must remain separate bodies. Each vertex's incident corners
    # must also form one connected fan, rejecting pinched manifold violations.
    paired = np.argsort(edge_inverse).reshape(-1, 2)
    for index, pair in enumerate(paired):
        left, right = int(pair[0]), int(pair[1])
        a, b = left % count, right % count
        ca, cb = left // count, right // count
        union(parents, a, b)
        union(fans, a * 3 + ca, b * 3 + (cb + 1) % 3)
        union(fans, a * 3 + (ca + 1) % 3, b * 3 + cb)
        if check and index % 4096 == 0:
            check()
    bodies = len({find(parents, i) for i in range(count)})
    if bodies != 1:
        raise ValueError("STL must contain exactly one face-connected body")
    if len({find(fans, i) for i in range(count * 3)}) != len(vertices):
        raise ValueError("STL contains a disconnected incident-face fan at a vertex")
    relative = triangles - xyz.mean(axis=0)
    volume = float(np.einsum("ij,ij->i", relative[:, 0], np.cross(relative[:, 1], relative[:, 2])).sum() / 6.0)
    if not math.isfinite(volume) or volume <= 0:
        raise ValueError("STL must have finite positive signed volume")
    if check:
        check()
    return {"bytes": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "faces": count, "vertices": len(vertices), "bodies": bodies,
            "closed": True, "consistent_winding": True, "volume": volume,
            "bounds": [xyz.min(axis=0).tolist(), xyz.max(axis=0).tolist()],
            "modified": False}


def validate_png(path, *, max_bytes):
    path = Path(path)
    if not 8 < path.stat().st_size <= max_bytes:
        raise ValueError("PNG is empty or exceeds the output budget")
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    offset = 8
    chunks = []
    payload = bytearray()
    dimensions = None
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG payload")
        body = data[offset + 8:end - 4]
        checksum = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + body) & 0xFFFFFFFF != checksum:
            raise ValueError("PNG checksum mismatch")
        chunks.append(kind)
        if kind == b"IHDR":
            if len(chunks) != 1 or length != 13:
                raise ValueError("invalid PNG header")
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
            if not channels or bits not in (8, 16) or not 0 < width <= 4096 or not 0 < height <= 4096 or compression or filtering or interlace:
                raise ValueError("unsupported PNG format or dimensions")
            expected = height * (1 + width * channels * (bits // 8))
            if expected > MAX_PNG_DECODED:
                raise ValueError("PNG decoded image exceeds budget")
            dimensions = (width, height, expected)
        elif kind == b"IDAT":
            payload.extend(body)
        elif kind == b"IEND":
            if length or end != len(data):
                raise ValueError("invalid PNG end")
        offset = end
    if not dimensions or chunks[-1:] != [b"IEND"] or not payload:
        raise ValueError("incomplete PNG")
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(payload, dimensions[2] + 1)
    except zlib.error as exc:
        raise ValueError("invalid PNG compressed data") from exc
    if len(decoded) != dimensions[2] or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid PNG image data")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "width": dimensions[0], "height": dimensions[1]}
