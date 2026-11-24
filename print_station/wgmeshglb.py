"""
Read a GLB, so a model the World Generator made can be dropped straight in.

The print station takes STL, 3MF, OBJ and PLY. A GLB is what the mesh
makers hand over, so the app reads one itself instead of asking the
person to convert it somewhere else first.

Two things about the format matter here:

* The numbers are in METRES, and a printer wants millimetres, so the
  model is scaled on the way in.
* The file stands Y-UP and a printer builds Z-UP, so the model is turned
  over. The turn was measured against Blender on this machine: Blender
  reads glTF (x, y, z) as (x, -z, y), and this does the same.

The reader is standard library only, so it works on a machine with
nothing else installed.
"""

from __future__ import annotations

import json
import struct


# The component types a glTF accessor can use, and how wide each one is.
COMPONENTS = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I",
               5126: "f"}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

# glTF is metres. A printer is millimetres.
METRES_TO_MM = 1000.0

def _chunks(blob: bytes):
    """The JSON part and the binary part of a GLB."""
    if len(blob) < 12:
        raise ValueError("that file is too short to be a GLB")
    magic, version, length = struct.unpack_from("<III", blob, 0)
    if magic != 0x46546C67:
        raise ValueError("that file is not a GLB")
    offset = 12
    document = None
    binary = b""
    while offset + 8 <= len(blob):
        size, kind = struct.unpack_from("<I4s", blob, offset)
        offset += 8
        body = blob[offset:offset + size]
        offset += size
        if kind == b"JSON":
            document = json.loads(body.decode("utf-8"))
        elif kind[:3] == b"BIN":
            binary = body
    if document is None:
        raise ValueError("that GLB has no scene in it")
    return document, binary


def _accessor(document, binary, index):
    """One accessor, as a flat list of numbers."""
    accessors = document.get("accessors") or []
    views = document.get("bufferViews") or []
    if index >= len(accessors):
        raise ValueError("the GLB points at a number that is not there")
    accessor = accessors[index]
    code = COMPONENTS.get(accessor.get("componentType"))
    if code is None:
        raise ValueError("the GLB uses a number format this app does not know")
    width = WIDTHS.get(accessor.get("type", "SCALAR"), 1)
    view = views[accessor["bufferView"]]
    base = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    size = struct.calcsize("<" + code) * width
    stride = int(view.get("byteStride") or 0)
    count = int(accessor.get("count", 0))
    out = []
    if stride and stride != size:
        for item in range(count):
            out.extend(struct.unpack_from("<" + code * width, binary,
                                         base + item * stride))
    else:
        out = list(struct.unpack_from("<" + code * width * count, binary, base))
    if accessor.get("normalized") and code in ("B", "H"):
        top = 255.0 if code == "B" else 65535.0
        out = [value / top for value in out]
    return out

def _vertex_colours(document, binary, index, count):
    """The colour on every vertex, as three numbers from 0 to 255."""
    if index is None:
        return None
    accessors = document.get("accessors") or []
    if index >= len(accessors):
        return None
    accessor = accessors[index]
    width = WIDTHS.get(accessor.get("type", "SCALAR"), 1)
    if width < 3:
        return None
    try:
        raw = _accessor(document, binary, index)
    except Exception:
        return None
    out = []
    for item in range(count):
        base = item * width
        if base + 2 >= len(raw):
            break
        # glTF COLOR_0 is linear, including normalized integer attributes.
        rgb = []
        for value in raw[base:base + 3]:
            value = max(0.0, min(1.0, float(value)))
            srgb = value * 12.92 if value <= 0.0031308 else (
                1.055 * value ** (1.0 / 2.4) - 0.055)
            rgb.append(int(round(srgb * 255.0)))
        out.append(tuple(rgb))
    return out or None


def _face_colour(vertex_colours, a, b, c):
    """The colour of a face: the average of the corners that make it."""
    if not vertex_colours:
        return (200, 200, 200)
    parts = []
    for index in (a, b, c):
        if 0 <= index < len(vertex_colours):
            parts.append(vertex_colours[index])
    if not parts:
        return (200, 200, 200)
    return (int(round(sum(part[0] for part in parts) / float(len(parts)))),
            int(round(sum(part[1] for part in parts) / float(len(parts)))),
            int(round(sum(part[2] for part in parts) / float(len(parts)))))


def _transform(node):
    """The 4x4 matrix of a node, as sixteen numbers in a row."""
    matrix = node.get("matrix")
    if matrix and len(matrix) == 16:
        return [float(v) for v in matrix]
    translation = node.get("translation") or [0.0, 0.0, 0.0]
    rotation = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]
    scale = node.get("scale") or [1.0, 1.0, 1.0]
    x, y, z, w = [float(v) for v in rotation]
    sx, sy, sz = [float(v) for v in scale]
    # The rotation a quaternion stands for, as a matrix.
    r = [
        1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0,
        2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0,
        2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0,
        0, 0, 0, 1,
    ]
    out = [0.0] * 16
    for row in range(3):
        for col in range(3):
            out[row * 4 + col] = r[row * 4 + col] * (sx, sy, sz)[col]
    out[12] = float(translation[0])
    out[13] = float(translation[1])
    out[14] = float(translation[2])
    out[15] = 1.0
    return out


def _multiply(a, b):
    """Two matrices, multiplied."""
    out = [0.0] * 16
    for row in range(4):
        for col in range(4):
            total = 0.0
            for k in range(4):
                total += a[row * 4 + k] * b[k * 4 + col]
            out[row * 4 + col] = total
    return out


def _apply(matrix, point):
    """One point, moved by a matrix."""
    x, y, z = point
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )

def read_glb(blob: bytes, mesh_class, colours=None):
    """A GLB, as a mesh of triangles.

    The mesh class is passed in so this file does not have to know how
    the rest of the app holds geometry.

    When a list is passed in for the colours, the colour of every face
    is put into it. A model that carries its own colours - the kind the
    mesh makers hand over - has them on the vertices, and the face takes
    the average of the three corners that make it.
    """
    document, binary = _chunks(blob)
    meshes = document.get("meshes") or []
    nodes = document.get("nodes") or []
    scenes = document.get("scenes") or []

    mesh = mesh_class()
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]

    roots = []
    if scenes:
        index = int(document.get("scene", 0))
        if index < len(scenes):
            roots = list(scenes[index].get("nodes") or [])
    if not roots:
        roots = list(range(len(nodes)))

    for index in roots:
        _walk(mesh, document, binary, nodes, meshes, index, identity, 0,
              colours)

    # The file is Y-UP in metres. A printer is Z-UP in millimetres.
    return mesh


def _walk(mesh, document, binary, nodes, meshes, index, parent, depth,
           colours=None):
    """Every node of the scene, with the transform it inherits."""
    if depth > 8 or index >= len(nodes):
        return
    node = nodes[index]
    world = _multiply(parent, _transform(node))
    if "mesh" in node and node["mesh"] < len(meshes):
        _add_mesh(mesh, document, binary, meshes[node["mesh"]], world, colours)
    for child in node.get("children") or []:
        _walk(mesh, document, binary, nodes, meshes, child, world, depth + 1,
              colours)


def _add_mesh(mesh, document, binary, entry, matrix, colours=None):
    """The triangles of one mesh, moved by the matrix."""
    for primitive in entry.get("primitives") or []:
        attributes = primitive.get("attributes") or {}
        if "POSITION" not in attributes:
            continue
        if int(primitive.get("mode", 4)) != 4:
            # Only triangles are a surface. Points and lines are not.
            continue
        positions = _accessor(document, binary, attributes["POSITION"])
        vertex_colours = None
        if colours is not None:
            vertex_colours = _vertex_colours(document, binary,
                                            attributes.get("COLOR_0"),
                                            len(positions) // 3)
        if primitive.get("indices") is None:
            indices = list(range(len(positions) // 3))
        else:
            indices = _accessor(document, binary, primitive["indices"])
        count = len(positions) // 3
        for k in range(0, len(indices) - 2, 3):
            a, b, c = indices[k], indices[k + 1], indices[k + 2]
            if not (0 <= a < count and 0 <= b < count and 0 <= c < count):
                continue
            corners = []
            for index in (a, b, c):
                point = _apply(matrix, (positions[index * 3],
                                         positions[index * 3 + 1],
                                         positions[index * 3 + 2]))
                # Y-UP to Z-UP, and metres to millimetres.
                corners.append((point[0] * METRES_TO_MM,
                                -point[2] * METRES_TO_MM,
                                point[1] * METRES_TO_MM))
            colour = None
            if colours is not None:
                colour = _face_colour(vertex_colours, a, b, c)
                colours.append(colour)
            mesh.triangle(corners[0], corners[1], corners[2])
