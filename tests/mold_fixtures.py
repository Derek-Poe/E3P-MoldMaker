"""Tiny analytic artifacts; no Blender, downloaded meshes, or device access."""

import struct
import zlib
from pathlib import Path

VERTICES = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
FACES = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def stl(path, vertices=None, faces=None):
    vertices = VERTICES if vertices is None else vertices
    faces = FACES if faces is None else faces
    with Path(path).open("wb") as output:
        output.write(b"Synthetic tetrahedron".ljust(80, b"\0"))
        output.write(struct.pack("<I", len(faces)))
        for face in faces:
            values = [coordinate for index in face for coordinate in vertices[index]]
            output.write(struct.pack("<12fH", 0, 0, 0, *values, 0))


def png(path):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\x10\x20\x30\xff")) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)
