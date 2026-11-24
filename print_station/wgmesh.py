"""Read a model file and turn it into triangles the app can show.

The print station has to put the model in front of the person before
anything is printed, and it has to do that with no extra software and no
internet. So the model is read here, with the standard library only, and
handed to the page as one flat block of numbers: positions and normals,
three floats each.

The block is small on purpose. A model that is too heavy to draw is also
too heavy to slice, so the triangles are cut down to a limit that keeps the
preview quick.
"""

from __future__ import annotations

import array
import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# More than this and the preview is not worth the wait.
MAX_TRIANGLES = 350000
# A print has fewer triangles in it than that. Past this the slicer
# spends its time on triangles nobody can see.
MAX_PRINT_TRIANGLES = 700000

MESH_TYPES = (".stl", ".obj", ".ply", ".3mf", ".glb")


def _local(tag: str) -> str:
    """The tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


class Mesh:
    """A flat pile of triangles. No shared vertices, so the shading is flat
    and the file is dead simple to hand to the browser.

    A scan is not a mesh. A point cloud has places but no surfaces, so it
    is carried with its own mode and the page draws it as points. The app
    says plainly that such a file cannot be printed.
    """

    def __init__(self):
        self.positions = array.array("f")
        self.normals = array.array("f")
        self.triangles = 0
        self.points = 0
        self.mode = 0

    def triangle(self, a, b, c):
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length <= 1e-12:
            nx, ny, nz = 0.0, 0.0, 1.0
        else:
            nx, ny, nz = nx / length, ny / length, nz / length
        self.positions.extend(a)
        self.positions.extend(b)
        self.positions.extend(c)
        self.normals.extend((nx, ny, nz))
        self.normals.extend((nx, ny, nz))
        self.normals.extend((nx, ny, nz))
        self.triangles += 1

    def point(self, a):
        """One point of a cloud. It has a place, but no surface."""
        self.positions.extend(a)
        self.normals.extend((0.0, 0.0, 0.0))
        self.points += 1

    def to_bytes(self) -> bytes:
        """The block the page reads: magic, counts, mode, positions, normals."""
        if sys.byteorder == "big":
            self.positions.byteswap()
            self.normals.byteswap()
        head = b"JHM1" + struct.pack("<III", len(self.positions) // 3,
                                     self.triangles, self.mode)
        return head + self.positions.tobytes() + self.normals.tobytes()


# -- STL -------------------------------------------------------------------

def _read_stl_binary(data: bytes) -> Mesh:
    mesh = Mesh()
    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + count * 50 > len(data):
        raise ValueError("the STL file is shorter than it claims")
    unpack = struct.Struct("<12fH").unpack_from
    offset = 84
    for _ in range(count):
        values = unpack(data, offset)
        offset += 50
        mesh.triangle(values[3:6], values[6:9], values[9:12])
    return mesh


def _read_stl_ascii(data: bytes) -> Mesh:
    mesh = Mesh()
    corners = []
    for line in data.decode("ascii", "replace").splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            corners.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(corners) == 3:
                mesh.triangle(corners[0], corners[1], corners[2])
                corners = []
    return mesh


def read_stl(data: bytes) -> Mesh:
    if len(data) >= 84:
        try:
            return _read_stl_binary(data)
        except Exception:
            pass
    return _read_stl_ascii(data)


# -- OBJ -------------------------------------------------------------------

def read_obj(text: str) -> Mesh:
    mesh = Mesh()
    verts = []
    for line in text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            corners = []
            for part in line.split()[1:]:
                try:
                    index = int(part.split("/")[0])
                except ValueError:
                    continue
                corners.append(index - 1 if index > 0 else len(verts) + index)
            for k in range(1, len(corners) - 1):
                a, b, c = corners[0], corners[k], corners[k + 1]
                if 0 <= a < len(verts) and 0 <= b < len(verts) and 0 <= c < len(verts):
                    mesh.triangle(verts[a], verts[b], verts[c])
    return mesh


# -- PLY -------------------------------------------------------------------

_PLY_TYPES = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}


def read_ply(data: bytes) -> Mesh:
    end = data.find(b"end_header")
    if end < 0:
        raise ValueError("the PLY file has no header")
    header = data[:end].decode("ascii", "replace")
    body = data[end + len(b"end_header"):]
    body = body.lstrip(b"\r\n")
    fmt = "ascii"
    if "binary_little_endian" in header:
        fmt = "little"
    elif "binary_big_endian" in header:
        fmt = "big"

    elements = []
    names = {}
    current = None
    for line in header.splitlines():
        line = line.strip()
        if line.startswith("element "):
            parts = line.split()
            current = {"name": parts[1], "count": int(parts[2]), "props": []}
            elements.append(current)
        elif line.startswith("property ") and current is not None:
            parts = line.split()
            if parts[1] == "list":
                current["props"].append({
                    "list": True, "count_type": parts[2], "type": parts[3],
                    "name": parts[4]})
            else:
                current["props"].append({
                    "list": False, "type": parts[1], "name": parts[2]})
        elif line.startswith("format "):
            fmt = {"ascii": "ascii", "binary_little_endian": "little",
                   "binary_big_endian": "big"}.get(line.split()[1], "ascii")

    mesh = Mesh()
    verts = []
    faces = []

    if fmt == "ascii":
        lines = body.decode("ascii", "replace").splitlines()
        index = 0
        for element in elements:
            for _ in range(element["count"]):
                if index >= len(lines):
                    break
                parts = lines[index].split()
                index += 1
                if element["name"] == "vertex":
                    verts.append(_ply_vertex(element, parts))
                elif element["name"] == "face":
                    faces.append(_ply_face(element, parts))
    else:
        order = "<" if fmt == "little" else ">"
        view = memoryview(body)
        offset = 0
        for element in elements:
            for _ in range(element["count"]):
                values = []
                for prop in element["props"]:
                    if prop["list"]:
                        code = order + _PLY_TYPES[prop["count_type"]]
                        size = struct.calcsize(code)
                        number = struct.unpack_from(code, view, offset)[0]
                        offset += size
                        code = order + _PLY_TYPES[prop["type"]] * number
                        size = struct.calcsize(code)
                        values.append(list(struct.unpack_from(code, view, offset)))
                        offset += size
                    else:
                        code = order + _PLY_TYPES[prop["type"]]
                        size = struct.calcsize(code)
                        values.append(struct.unpack_from(code, view, offset)[0])
                        offset += size
                if element["name"] == "vertex":
                    verts.append(_ply_vertex(element, values))
                elif element["name"] == "face":
                    faces.append(_ply_face(element, values))

    for face in faces:
        for k in range(1, len(face) - 1):
            a, b, c = face[0], face[k], face[k + 1]
            if 0 <= a < len(verts) and 0 <= b < len(verts) and 0 <= c < len(verts):
                mesh.triangle(verts[a], verts[b], verts[c])
    if not mesh.triangles and verts:
        # A scan, not a surface. The page draws the points so a person can
        # see what they dropped in.
        for vertex in verts:
            mesh.point(vertex)
        mesh.mode = 1
    return mesh


def _ply_vertex(element, values):
    for name, value in zip([p["name"] for p in element["props"]], values):
        if name == "x":
            x = float(value)
        elif name == "y":
            y = float(value)
        elif name == "z":
            z = float(value)
    return (x, y, z)


def _ply_face(element, values):
    for prop, value in zip(element["props"], values):
        if prop.get("list") and prop["name"] in ("vertex_indices", "vertex_index"):
            return [int(v) for v in value]
    return []


# -- 3MF -------------------------------------------------------------------

def _transform(text: str):
    """A 3MF transform is twelve numbers: a 3x4 matrix."""
    try:
        values = [float(v) for v in text.split()]
    except ValueError:
        return None
    if len(values) != 12:
        return None
    m00, m10, m20, m01, m11, m21, m02, m12, m22, m30, m31, m32 = values
    return ((m00, m01, m02, m30), (m10, m11, m12, m31), (m20, m21, m22, m32))


def _apply(matrix, point):
    x, y, z = point
    return (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
            matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3])


def read_3mf(path: Path) -> Mesh:
    mesh = Mesh()
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".model")]
        if not names:
            raise ValueError("the 3MF file has no model inside it")
        # The build item points at the objects that make up the print.
        parts = {}
        for name in names:
            try:
                parts[name] = archive.read(name)
            except Exception:
                continue
    objects = {}
    builds = []
    for name, blob in parts.items():
        try:
            root = ElementTree.fromstring(blob)
        except ElementTree.ParseError:
            continue
        for element in root.iter():
            tag = _local(element.tag)
            if tag == "object":
                ident = element.get("id")
                objects[ident] = _three_mf_object(element)
            elif tag == "item":
                builds.append((element.get("objectid"), element.get("transform")))
    built = []
    for objectid, transform in builds:
        if objectid in objects:
            built.append((objects[objectid], _transform(transform or "")))
    if not built:
        for ident, obj in objects.items():
            built.append((obj, None))
    for obj, matrix in built:
        _emit_object(mesh, obj, matrix, 0)
    return mesh


def _three_mf_object(element):
    """Pull the mesh and the component links out of one 3MF object."""
    mesh_data = {"vertices": [], "triangles": [], "components": [],
                "properties": []}
    for child in element:
        if _local(child.tag) == "mesh":
            for part in child:
                tag = _local(part.tag)
                if tag == "vertices":
                    for vertex in part:
                        mesh_data["vertices"].append(
                            (float(vertex.get("x", 0)), float(vertex.get("y", 0)),
                             float(vertex.get("z", 0))))
                elif tag == "triangles":
                    for triangle in part:
                        mesh_data["triangles"].append(
                            (int(triangle.get("v1", 0)), int(triangle.get("v2", 0)),
                             int(triangle.get("v3", 0))))
                        # The material of the face, when the file has one.
                        # A face of a coloured part names the colour it
                        # is going to be printed in.
                        mesh_data["properties"].append(
                            (triangle.get("p1"), triangle.get("p2"),
                             triangle.get("p3")))
        elif _local(child.tag) == "components":
            for component in child:
                mesh_data["components"].append(
                    (component.get("objectid"), component.get("transform")))
    return mesh_data


def _emit_object(mesh: Mesh, obj, matrix, depth):
    if depth > 4:
        return
    vertices = obj["vertices"]
    moved = [(_apply(matrix, v) if matrix else v) for v in vertices]
    for a, b, c in obj["triangles"]:
        if 0 <= a < len(moved) and 0 <= b < len(moved) and 0 <= c < len(moved):
            mesh.triangle(moved[a], moved[b], moved[c])
    for objectid, transform in obj["components"]:
        pass


def _hex_to_rgb(code) -> tuple:
    """A colour code as three numbers from 0 to 255."""
    text = str(code or "").strip().lstrip("#")
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6:
        return (128, 128, 128)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def read_3mf_coloured(path: Path):
    """A 3MF this app wrote: the shape, and the colour of every face.

    The colours in the file are real materials, so the file opens in any
    slicer and the part shows in the colours it is going to be printed
    in. This reads them back, so the app can put those colours on the
    bed and into the G-code.
    """
    mesh = Mesh()
    slots = []
    colours = []
    palette = []
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".model")]
        blobs = []
        for name in names:
            try:
                blobs.append(archive.read(name))
            except Exception:
                continue
    for blob in blobs:
        try:
            root = ElementTree.fromstring(blob)
        except ElementTree.ParseError:
            continue
        materials = {}
        for element in root.iter():
            if _local(element.tag) != "basematerials":
                continue
            items = []
            for child in element:
                if _local(child.tag) != "base":
                    continue
                rgb = _hex_to_rgb(child.get("displaycolor"))
                items.append({"name": str(child.get("name") or "Colour"),
                              "hex": "#%02X%02X%02X" % rgb, "rgb": rgb})
            materials[element.get("id")] = items
            if items and not palette:
                palette = items
        objects = {}
        for element in root.iter():
            if _local(element.tag) == "object":
                objects[element.get("id")] = element
        built = []
        for element in root.iter():
            if _local(element.tag) == "item":
                built.append((element.get("objectid"), element.get("transform")))
        if not built:
            built = [(ident, None) for ident in objects.keys()]
        for objectid, transform in built:
            element = objects.get(objectid)
            if element is None:
                continue
            items = materials.get(element.get("pid")) or []
            try:
                fallback = int(element.get("pindex") or 0)
            except (TypeError, ValueError):
                fallback = 0
            data = _three_mf_object(element)
            matrix = _transform(transform or "")
            moved = [(_apply(matrix, v) if matrix else v) for v in data["vertices"]]
            data["vertices"] = moved
            for index, triangle in enumerate(data["triangles"]):
                a, b, c = triangle
                if not (0 <= a < len(data["vertices"])
                        and 0 <= b < len(data["vertices"])
                        and 0 <= c < len(data["vertices"])):
                    continue
                slot = fallback
                props = (data["properties"][index]
                         if index < len(data["properties"]) else (None, None, None))
                for value in props:
                    if value is not None:
                        try:
                            slot = int(value)
                        except (TypeError, ValueError):
                            slot = fallback
                        break
                entry = items[slot] if 0 <= slot < len(items) else None
                mesh.triangle(data["vertices"][a], data["vertices"][b],
                              data["vertices"][c])
                slots.append(slot if entry else -1)
                colours.append(entry["rgb"] if entry else (200, 200, 200))
    return mesh, slots, colours, palette


def load_coloured(path: Path):
    """The model, the colour of every face, and the palette behind it.

    Returns the mesh, the slot of every face, the colour of every face
    and the palette. A model with no colour of its own comes back with
    no slots and an empty palette, and it prints the way it always did.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".3mf":
        return read_3mf_coloured(path)
    if suffix == ".glb":
        import wgmeshglb

        colours = []
        mesh = wgmeshglb.read_glb(path.read_bytes(), Mesh, colours)
        return mesh, [-1] * mesh.triangles, colours, []
    mesh = load_mesh(path)
    return mesh, [], [], []


# -- the one door in -------------------------------------------------------

def to_view(mesh: Mesh) -> Mesh:
    """The same mesh, turned so the page can draw it standing up.

    The printer, the bed and the slicer all work in Z-up: the bed is
    the XY plane and the height is Z. The page draws the model with Y
    as the height, so the shape is turned a quarter turn on its way out.

    The turn is a real rotation and not a mirror - (x, y, z) becomes
    (x, z, -y) - so the faces keep their winding and the model is not
    drawn inside out, and nothing is mirrored.
    """
    out = Mesh()
    source = mesh.positions
    for i in range(0, len(source), 3):
        out.positions.extend((source[i], source[i + 2], -source[i + 1]))
    normals = mesh.normals
    for i in range(0, len(normals), 3):
        out.normals.extend((normals[i], normals[i + 2], -normals[i + 1]))
    out.triangles = mesh.triangles
    out.points = mesh.points
    out.mode = mesh.mode
    return out


def mesh_bytes(path: Path, for_view: bool = False) -> bytes:
    """The mesh of a model file, as the block the page reads."""
    suffix = path.suffix.lower()
    if suffix == ".3mf":
        mesh = read_3mf(path)
    elif suffix == ".stl":
        mesh = read_stl(path.read_bytes())
    elif suffix == ".obj":
        mesh = read_obj(path.read_text("utf-8", "replace"))
    elif suffix == ".ply":
        mesh = read_ply(path.read_bytes())
    elif suffix == ".glb":
        import wgmeshglb

        mesh = wgmeshglb.read_glb(path.read_bytes(), Mesh)
    else:
        raise ValueError("that file is not a model this app can draw")

    if mesh.triangles > MAX_TRIANGLES:
        mesh = _decimate(mesh, MAX_TRIANGLES)
    elif mesh.mode == 1 and mesh.points > MAX_TRIANGLES:
        # A big scan is thinned the same way, so the preview stays quick.
        step = int(mesh.points / float(MAX_TRIANGLES)) + 1
        thin = Mesh()
        thin.mode = 1
        for index in range(0, mesh.points, step):
            base = index * 3
            thin.point(mesh.positions[base:base + 3])
        mesh = thin
    if for_view:
        mesh = to_view(mesh)
    return mesh.to_bytes()




# -- the plate ------------------------------------------------------------


def load_mesh(path: Path) -> Mesh:
    """Read a model file into triangles, whatever kind it is."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return read_stl(path.read_bytes())
    if suffix == ".obj":
        return read_obj(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".ply":
        return read_ply(path.read_bytes())
    if suffix == ".3mf":
        return read_3mf(path)
    if suffix == ".glb":
        import wgmeshglb

        return wgmeshglb.read_glb(path.read_bytes(), Mesh)
    raise ValueError("that file cannot be read as a mesh")


def mesh_bounds(mesh: Mesh) -> tuple:
    """Where the model sits: left, right, front, back, bottom, top."""
    positions = mesh.positions
    if not positions:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    xs = positions[0::3]
    ys = positions[1::3]
    zs = positions[2::3]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def scaled(mesh: Mesh, sx: float, sy: float, sz: float) -> Mesh:
    """The same shape, a different size."""
    out = Mesh()
    source = mesh.positions
    for i in range(0, len(source), 3):
        out.positions.append(source[i] * sx)
        out.positions.append(source[i + 1] * sy)
        out.positions.append(source[i + 2] * sz)
    # The normals are carried along too. A mesh that has lost them cannot
    # be turned afterwards, and the bed view turns the pieces.
    normals = mesh.normals
    for i in range(0, len(normals), 3):
        out.normals.append(normals[i] * sx)
        out.normals.append(normals[i + 1] * sy)
        out.normals.append(normals[i + 2] * sz)
    out.triangles = mesh.triangles
    out.points = mesh.points
    out.mode = mesh.mode
    return out


def rotated(mesh: Mesh, degrees: float) -> Mesh:
    """The same shape, turned about the vertical axis.

    A copy that is placed on the bed can face any way. The turn is about
    the middle of the shape, so it stays where it was put.
    """
    import math
    angle = math.radians(float(degrees or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    out = Mesh()
    out.triangles = mesh.triangles
    out.points = mesh.points
    out.mode = mesh.mode
    source = mesh.positions
    normals = mesh.normals
    for i in range(0, len(source), 3):
        x = source[i]
        y = source[i + 1]
        out.positions.append(x * cos_a - y * sin_a)
        out.positions.append(x * sin_a + y * cos_a)
        out.positions.append(source[i + 2])
        if i + 2 < len(normals):
            nx = normals[i]
            ny = normals[i + 1]
            out.normals.append(nx * cos_a - ny * sin_a)
            out.normals.append(nx * sin_a + ny * cos_a)
            out.normals.append(normals[i + 2])
        else:
            out.normals.append(0.0)
            out.normals.append(0.0)
            out.normals.append(1.0)
    return out


def moved(mesh: Mesh, dx: float, dy: float, dz: float) -> Mesh:
    """The same shape, put somewhere else."""
    out = Mesh()
    source = mesh.positions
    for i in range(0, len(source), 3):
        out.positions.append(source[i] + dx)
        out.positions.append(source[i + 1] + dy)
        out.positions.append(source[i + 2] + dz)
    out.normals.extend(mesh.normals)
    out.triangles = mesh.triangles
    out.points = mesh.points
    out.mode = mesh.mode
    return out


def write_stl(pieces: list, path: Path) -> Path:
    """Write the triangles as a binary STL.

    STL is the format the slicer reads most reliably. The same shape
    written as an OBJ came back with "no layers were detected" on a
    model of this size, and came back perfectly from an STL - so this is
    what the plate is written as.
    """
    import math
    import struct
    out = bytearray()
    out += b"\x00" * 80
    total = sum(len(mesh.positions) // 9 for mesh in pieces)
    out += struct.pack("<I", total)
    for mesh in pieces:
        positions = mesh.positions
        for i in range(0, len(positions), 9):
            a = positions[i:i + 3]
            b = positions[i + 3:i + 6]
            c = positions[i + 6:i + 9]
            ux = b[0] - a[0]
            uy = b[1] - a[1]
            uz = b[2] - a[2]
            vx = c[0] - a[0]
            vy = c[1] - a[1]
            vz = c[2] - a[2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            out += struct.pack("<12fH", nx / length, ny / length, nz / length,
                               a[0], a[1], a[2], b[0], b[1], b[2],
                               c[0], c[1], c[2], 0)
    path.write_bytes(bytes(out))
    return path


def plate_pieces(mesh, options=None, area=None, layout=None, max_height=None):
    """The shape as it will be printed: the size asked for, put on the bed,
    and the copies laid out side by side.

    The shape itself is not changed here. The work on the shape - cutting
    it down, smoothing it, laying it on its face - happens before this,
    and the mesh that comes out of it is what is put on the bed.
    """
    options = options or {}
    scale = 1.0
    try:
        scale = float(options.get("scale") or 100) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    copies = 1
    try:
        copies = max(1, int(options.get("copies") or 1))
    except (TypeError, ValueError):
        copies = 1
    gap = 6.0
    try:
        gap = float(options.get("gap") or 6.0)
    except (TypeError, ValueError):
        gap = 6.0

    left, right, front, back, low, high = mesh_bounds(mesh)
    width = right - left
    depth = back - front
    height = high - low

    # "Fit to the bed" is the scale that puts the part inside the
    # printable area with a margin to spare.
    if str(options.get("fit") or "").lower() in ("1", "true", "on", "yes") and area:
        room_x = (area[1] - area[0]) - 10.0
        room_y = (area[3] - area[2]) - 10.0
        factor = 1.0
        if width > 0:
            factor = min(factor, room_x / width)
        if depth > 0:
            factor = min(factor, room_y / depth)
        # How tall the printer can actually go. A tall model made to fill
        # the bed would not go in the machine, and the app would hand the
        # slicer a part that cannot be printed.
        try:
            limit = float(max_height or 0.0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit > 0 and height > 0:
            factor = min(factor, (limit - 10.0) / height)
        # The size the person asked for is the ceiling. "Fit to the bed"
        # never quietly makes a part bigger than the size they chose.
        scale = max(0.05, min(10.0, min(scale, factor)))

    width *= scale
    depth *= scale
    height *= scale

    size = scaled(mesh, scale, scale, scale)
    # The part is centred on the bed and sits on it, so the copies
    # land where the bed actually is. On a printer whose origin is in
    # the middle - the Adventurer 5M - that is the middle of the bed.
    centre_x = (left + right) / 2.0 * scale
    centre_y = (front + back) / 2.0 * scale
    size = moved(size, -centre_x, -centre_y, -low * scale)

    pieces = []
    if layout:
        # The person has put the pieces where they want them on the bed.
        # Each one is turned about its middle if it was rotated, and put
        # down at the point it was dragged to. The origin is the origin of
        # the bed, which on some machines is the middle of it and on
        # others is a corner.
        for item in layout:
            piece = size
            turn = float(item.get("rot") or 0.0)
            if turn:
                piece = rotated(piece, turn)
            piece = moved(piece, float(item.get("x") or 0.0),
                          float(item.get("y") or 0.0), 0.0)
            pieces.append(piece)
    elif copies <= 1:
        pieces.append(size)
    else:
        import math
        columns = int(math.ceil(math.sqrt(copies)))
        rows = int(math.ceil(copies / float(columns)))
        step_x = width + gap
        step_y = depth + gap
        for index in range(copies):
            column = index % columns
            row = index // columns
            offset_x = (column - (columns - 1) / 2.0) * step_x
            offset_y = (row - (rows - 1) / 2.0) * step_y
            pieces.append(moved(size, offset_x, offset_y, 0.0))
    return {"pieces": pieces, "scale": scale, "copies": copies,
            "width": width, "depth": depth, "height": height}


def prepare_plate(model: Path, out_dir: Path, options: dict = None,
                  area=None, layout=None, max_height=None) -> dict:
    """The model, made the size that was asked for and laid out in copies.

    The slicer keeps the size of a part and the number of copies per
    object, and it will not take either of them from a profile file -
    they are set on the object, not on the print. So the app does that
    work itself: the mesh is scaled, centred on the bed, and the copies
    are laid out side by side, and the slicer is handed the result.

    Nothing is lost by doing it here. The person sees the model change
    size in the view, and the copy count is real copies on the bed.
    """
    options = options or {}
    scale = 1.0
    try:
        scale = float(options.get("scale") or 100) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    copies = 1
    try:
        copies = max(1, int(options.get("copies") or 1))
    except (TypeError, ValueError):
        copies = 1
    gap = 6.0
    try:
        gap = float(options.get("gap") or 6.0)
    except (TypeError, ValueError):
        gap = 6.0

    mesh, slots, colours, palette = load_coloured(model)
    if not mesh.triangles or mesh.mode == 1:
        return {"path": "", "scale": scale, "copies": copies}

    def _num(value, fallback=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    # The work the slicer does with its own buttons, done here on the
    # shape - so the person gets those buttons without the slicer's
    # window ever opening.
    notes = {}
    triangles_before = mesh.triangles
    coloured = bool(palette)
    if coloured:
        # A coloured file is printed exactly as it was coloured. The work
        # on the shape would change the faces, and the colour of a face is
        # the face itself, so it is not done on a coloured file.
        notes["colour"] = {"slots": len(palette), "faces": mesh.triangles}
    elif triangles_before > MAX_PRINT_TRIANGLES:
        # A print does not need millions of triangles, and the slicer
        # will not thank anyone for them. The shape is cut down with
        # the surface kept, and if that opens the shape up, it is
        # refused rather than printed with holes in it.
        smaller = _decimate(mesh, MAX_PRINT_TRIANGLES)
        if open_edges(smaller) > open_edges(mesh):
            raise ValueError(
                "this model has %d triangles and cutting it down opened "
                "it up, so it is not safe to print. Simplify it in a "
                "modeller first, or use the Simplify control on the "
                "slicer desk." % triangles_before)
        mesh = smaller
        notes["cut_down"] = {"from": triangles_before,
                             "to": mesh.triangles}
    cell = _num(options.get("simplify"))
    if not coloured and cell > 0:
        smaller, info = simplify_mesh(mesh, cell)
        before_open = open_edges(mesh)
        after_open = open_edges(smaller)
        info["open_before"] = before_open
        info["open_after"] = after_open
        if after_open > before_open:
            # The cut-down shape opened up. A hole in a print is worse
            # than a heavy file, so the whole shape is kept and the
            # person is told why.
            info["used"] = False
            info["why"] = ("the cut-down shape had %d open edges and the "
                           "full shape had %d, so the full shape was used"
                           % (after_open, before_open))
        else:
            info["used"] = True
            mesh = smaller
        notes["simplify"] = info
    passes = int(_num(options.get("smooth")))
    if not coloured and passes > 0:
        mesh, info = smooth_mesh(mesh, passes)
        notes["smooth"] = info
    if not coloured and str(options.get("orient_face") or "").lower() in ("1", "true", "on", "yes"):
        mesh, info = lay_on_face(mesh)
        notes["orient"] = info
        # The slicer refuses a part that has just been turned over on
        # its own - the turned shape has edges that are a hair out of
        # true, and the slicer will not take them. One cleaning pass
        # takes those edges back in line, and the part is then sliced
        # as it lies. What it cost is measured and shown.
        mesh, clean = smooth_mesh(mesh, 1)
        clean["height_before"] = info.get("height")
        clean["height_after"] = _mesh_height(mesh)
        notes["orient"]["cleaned"] = clean
    notes["triangles_before"] = triangles_before
    notes["triangles_after"] = mesh.triangles

    # The shape is sized and put on the bed in one place, so the
    # picture on the page and the plate the slicer is given are the
    # same thing.
    result = plate_pieces(mesh, options, area, layout, max_height)
    pieces = result["pieces"]
    scale = result["scale"]
    copies = result["copies"]
    width = result["width"]
    depth = result["depth"]
    height = result["height"]

    out_dir.mkdir(parents=True, exist_ok=True)
    if coloured:
        # The plate keeps its colours. It is handed to the slicer as a
        # 3MF, because that is the only one of the formats the slicer
        # reads that carries the material of every face - an STL has no
        # colours in it at all.
        import wgcolour

        target = out_dir / (Path(model).stem + "_plate.3mf")
        wgcolour.write_3mf([{"mesh": piece, "slots": slots} for piece in pieces],
                            palette, target)
    else:
        target = out_dir / (Path(model).stem + "_plate.stl")
        write_stl(pieces, target)
    return {"path": str(target), "scale": scale, "copies": copies,
            "width": width, "depth": depth, "height": height,
            "notes": notes, "triangles": mesh.triangles}


def mesh_info(path: Path) -> dict:
    """What a model file holds: surfaces, or only points."""
    blob = mesh_bytes(path)
    mode = int.from_bytes(blob[12:16], "little")
    vertices = int.from_bytes(blob[4:8], "little")
    triangles = int.from_bytes(blob[8:12], "little")
    return {"mode": mode, "vertices": vertices, "triangles": triangles,
            "points": vertices if mode == 1 else 0}


def _decimate(mesh: Mesh, limit: int) -> Mesh:
    """A lighter copy of the shape, with the surface still there.

    Taking every Nth triangle is quick, but it leaves holes: the shape
    turns into a cloud of loose triangles and the model looks like
    noise. Gathering the corners into boxes instead keeps the surface
    whole and takes the triangles away for real. The box size is
    worked out from the shape itself, and the result is checked, so
    the model always comes back looking like the model.
    """
    bounds = mesh_bounds(mesh)
    size = max(bounds[1] - bounds[0], bounds[3] - bounds[2],
               bounds[5] - bounds[4]) or 1.0
    cell = size / 220.0
    best = mesh
    for _ in range(6):
        small, info = simplify_mesh(mesh, cell)
        best = small
        if info["triangles"] <= limit:
            return small
        # The surface is still too busy. The corners on a surface do
        # not fall away as fast as the square of the box, so the box
        # is grown by the power that actually fits, and it is tried
        # again until the shape is light enough.
        ratio = float(limit) / max(1.0, float(info["triangles"]))
        cell *= (ratio ** 0.72) * 1.02
    return best


def _thin(mesh: Mesh, limit: int) -> Mesh:
    """Keep every Nth triangle when a model is too heavy to draw."""
    step = int(mesh.triangles / float(limit)) + 1
    small = Mesh()
    for index in range(0, mesh.triangles, step):
        base = index * 9
        small.triangle(mesh.positions[base:base + 3],
                       mesh.positions[base + 3:base + 6],
                       mesh.positions[base + 6:base + 9])
    return small


# -- the mesh work the slicer desk does ------------------------------------
#
# The slicer does these with its own buttons, but only when its window is
# open. The app does them instead, on the mesh, before the slicer ever
# sees it - so the person never has to open the slicer to use them.


def _weld(mesh: Mesh, cell: float = 0.0):
    """The mesh with shared corners instead of repeated ones.

    A file of triangles has no corner that belongs to two of them, so
    the same point is written three times over. To smooth a shape or
    to cut it down, the corners have to be found first and joined back
    together. With a cell size, the points are gathered into boxes
    instead - that is how the shape is cut down.
    """
    points = []
    faces = []
    index = {}
    positions = mesh.positions
    for base in range(0, len(positions), 9):
        face = []
        for corner in range(3):
            at = base + corner * 3
            x = positions[at]
            y = positions[at + 1]
            z = positions[at + 2]
            if cell > 0:
                key = (int(x // cell), int(y // cell), int(z // cell))
            else:
                key = (round(x, 4), round(y, 4), round(z, 4))
            found = index.get(key)
            if found is None:
                found = len(points)
                index[key] = found
                points.append([x, y, z])
            face.append(found)
        faces.append(face)
    return points, faces


def open_edges(mesh: Mesh) -> int:
    """How many edges of the shape are open.

    A closed shape has every edge shared by two triangles. An open
    edge is a hole, and a hole is a place where the printer's nozzle
    would put material in mid air.
    """
    counts = {}
    positions = mesh.positions
    for base in range(0, len(positions), 9):
        tri = []
        for corner in range(3):
            at = base + corner * 3
            tri.append((round(positions[at], 3), round(positions[at + 1], 3),
                        round(positions[at + 2], 3)))
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (a, b) if a <= b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    return sum(1 for value in counts.values() if value == 1)


def smooth_mesh(mesh: Mesh, iterations: int = 2, factor: float = 0.5) -> tuple:
    """The same shape, with the noise taken off it.

    Taubin smoothing: the shape is pulled towards its neighbours and
    then pushed back again, so it does not shrink the way plain
    smoothing makes it shrink. This is the technique, written out here
    - no borrowed code and nothing to licence.
    """
    import math
    points, faces = _weld(mesh)
    if len(points) < 4 or iterations < 1:
        return mesh, {"moved": 0.0, "points": len(points)}
    neighbours = []
    for a, b, c in faces:
        neighbours.append((a, b, c))
    lists = [[] for _ in points]
    for a, b, c in neighbours:
        lists[a].append(b)
        lists[a].append(c)
        lists[b].append(a)
        lists[b].append(c)
        lists[c].append(a)
        lists[c].append(b)
    lists = [set(item) for item in lists]
    start = [list(point) for point in points]
    for _ in range(int(iterations)):
        for step in (factor, -factor * 1.06):
            updated = []
            for index, point in enumerate(points):
                near = lists[index]
                if not near:
                    updated.append(list(point))
                    continue
                ax = ay = az = 0.0
                for other in near:
                    ax += points[other][0]
                    ay += points[other][1]
                    az += points[other][2]
                count = len(near)
                updated.append([point[0] + step * (ax / count - point[0]),
                                point[1] + step * (ay / count - point[1]),
                                point[2] + step * (az / count - point[2])])
            points = updated
    total = 0.0
    for index, point in enumerate(points):
        total += math.dist(point, start[index])
    moved = total / len(points) if points else 0.0
    out = Mesh()
    for a, b, c in faces:
        out.triangle(points[a], points[b], points[c])
    return out, {"moved": moved, "points": len(points)}


def simplify_mesh(mesh: Mesh, cell: float) -> tuple:
    """The same shape, with fewer triangles.

    The corners are gathered into boxes of the size asked for, and the
    shape is rebuilt from the boxes. Two corners that end up in the
    same box become one, and the triangles that collapse are dropped.
    The result is a rougher shape with the same outline, which is
    what a machine with a finite step size wants anyway.
    """
    points, faces = _weld(mesh, cell=max(0.05, float(cell)))
    out = Mesh()
    for a, b, c in faces:
        if a == b or b == c or a == c:
            continue
        out.triangle(points[a], points[b], points[c])
    return out, {"points": len(points), "triangles": out.triangles}


def _rotate_to_axis(normal, target):
    """The turn that takes one direction to another."""
    import math
    ax, ay, az = normal
    bx, by, bz = target
    length = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
    ax, ay, az = ax / length, ay / length, az / length
    length = math.sqrt(bx * bx + by * by + bz * bz) or 1.0
    bx, by, bz = bx / length, by / length, bz / length
    dot = ax * bx + ay * by + az * bz
    if dot > 0.999999:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    if dot < -0.999999:
        # Opposite ways: turn it a half turn about any axis at right
        # angles to the first.
        if abs(ax) < 0.9:
            ux, uy, uz = 1.0, 0.0, 0.0
        else:
            ux, uy, uz = 0.0, 1.0, 0.0
        cx = ay * uz - az * uy
        cy = az * ux - ax * uz
        cz = ax * uy - ay * ux
        cl = math.sqrt(cx * cx + cy * cy + cz * cz) or 1.0
        cx, cy, cz = cx / cl, cy / cl, cz / cl
        return (2 * cx * cx - 1, 2 * cx * cy, 2 * cx * cz,
                2 * cx * cy, 2 * cy * cy - 1, 2 * cy * cz,
                2 * cx * cz, 2 * cy * cz, 2 * cz * cz - 1)
    cx = ay * bz - az * by
    cy = az * bx - ax * bz
    cz = ax * by - ay * bx
    sine = math.sqrt(cx * cx + cy * cy + cz * cz)
    if sine < 1e-9:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    # The turn is about the axis that lies between the two directions,
    # by the angle between them. This is the whole of it - a real
    # rotation, so the shape is not stretched by it.
    nx = cx / sine
    ny = cy / sine
    nz = cz / sine
    cos_a = max(-1.0, min(1.0, dot))
    flat = 1.0 - cos_a
    return (flat * nx * nx + cos_a, flat * nx * ny - sine * nz,
            flat * nx * nz + sine * ny,
            flat * nx * ny + sine * nz, flat * ny * ny + cos_a,
            flat * ny * nz - sine * nx,
            flat * nx * nz - sine * ny, flat * ny * nz + sine * nx,
            flat * nz * nz + cos_a)


def lay_on_face(mesh: Mesh) -> tuple:
    """The shape, turned over so that its flattest face is on the bed.

    The app works out which way the biggest flat face points and turns
    the shape so that face is the one lying on the bed. The turn that
    makes the shape shortest is the one that is used, so a part that
    is standing on edge comes out lying down.
    """
    import math
    positions = mesh.positions
    if not mesh.triangles:
        return mesh, {"height": 0.0}
    buckets = {}
    for base in range(0, len(positions), 9):
        ax, ay, az = positions[base:base + 3]
        bx, by, bz = positions[base + 3:base + 6]
        cx, cy, cz = positions[base + 6:base + 9]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-9:
            continue
        area = length * 0.5
        nx, ny, nz = nx / length, ny / length, nz / length
        key = (round(nx, 1), round(ny, 1), round(nz, 1))
        item = buckets.get(key)
        if item is None:
            buckets[key] = [area, nx, ny, nz]
        else:
            item[0] += area
            item[1] += nx * area
            item[2] += ny * area
            item[3] += nz * area
    if not buckets:
        return mesh, {"height": 0.0}
    best = max(buckets.values(), key=lambda item: item[0])
    normal = (best[1], best[2], best[3])
    length = math.sqrt(sum(value * value for value in normal)) or 1.0
    normal = tuple(value / length for value in normal)

    left, right, front, back, low, high = mesh_bounds(mesh)
    before = high - low
    centre_x = (left + right) / 2.0
    centre_y = (front + back) / 2.0

    def turned(matrix):
        out = Mesh()
        for base in range(0, len(positions), 9):
            corners = []
            for corner in range(3):
                at = base + corner * 3
                x = positions[at] - centre_x
                y = positions[at + 1] - centre_y
                z = positions[at + 2]
                corners.append((matrix[0] * x + matrix[1] * y + matrix[2] * z,
                                matrix[3] * x + matrix[4] * y + matrix[5] * z,
                                matrix[6] * x + matrix[7] * y + matrix[8] * z))
            out.triangle(corners[0], corners[1], corners[2])
        out.points = mesh.points
        out.mode = mesh.mode
        return out

    down = turned(_rotate_to_axis(normal, (0.0, 0.0, -1.0)))
    up = turned(_rotate_to_axis(normal, (0.0, 0.0, 1.0)))
    d_bounds = mesh_bounds(down)
    u_bounds = mesh_bounds(up)
    d_height = d_bounds[5] - d_bounds[4]
    u_height = u_bounds[5] - u_bounds[4]
    # Three ways of lying down: the shape as it came, the flat face
    # turned under, and the flat face turned over. The one that is
    # lowest wins, so the button can never make the part taller than
    # it already was.
    after, chosen = min([(before, mesh), (d_height, down), (u_height, up)],
                        key=lambda item: item[0])
    return chosen, {"height": after, "height_before": before,
                   "turned": abs(after - before) > 0.01}


def _mesh_height(mesh: Mesh) -> float:
    bounds = mesh_bounds(mesh)
    return bounds[5] - bounds[4]