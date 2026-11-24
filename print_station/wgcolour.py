"""Reduce a model's colours to the few a printer can actually lay down.

A print file cannot carry every colour of a picture. A printer has a
fixed number of filaments - four on most AMS and MMU machines, up to
sixteen on the big ones, one on a plain single-spool machine - so the
file has to carry a PALETTE: the few colours that will be laid down,
and every piece of the model assigned to one of them.

The same job the Mesh Maker does with Blender, done here with the
standard library only, so the customer does not need Blender on their
machine to print their own model in colour.

Three steps, and the order matters:

1. REDUCE. The colours on the model are reduced to the number the
   printer has. K-means in RGB, started from a fixed seed, so the same
   model always comes out the same way - a print file that changes
   colour between runs is a print file nobody can repeat.

2. CLEAN. Remove only small, enclosed, similar-colour islands across
   shared mesh edges. Nearby but separate surfaces never vote on each
   other, and strong colour boundaries are kept.

3. WRITE. The result is a candidate 3MF with per-face materials.
   RGB clustering does not preserve semantic regions. Compare against
   the textured original in the external slicer before accepting it;
   material tags alone do not prove appearance or printable assignments.

A model can have hundreds of thousands of faces and only a handful of
colours, so the colours that are actually on the model are counted
first and the work is done on those. The answer is the same and it is
a fraction of the time.
"""

from __future__ import annotations

import math
import struct
import zipfile
from array import array
from pathlib import Path
from xml.sax.saxutils import escape


# The filaments a print file can name. The hex codes are the ones the
# slicers and the printer's own palette use, so the file matches the
# spool instead of guessing at it.
FILAMENTS = [
    ("Black", "#000000"),
    ("White", "#FFFFFF"),
    ("Grey", "#808080"),
    ("Silver", "#C0C0C0"),
    ("Red", "#C42B1C"),
    ("Orange", "#FB4C00"),
    ("Yellow", "#FFD400"),
    ("Green", "#00A95C"),
    ("Blue", "#1C69AA"),
    ("Purple", "#705697"),
    ("Brown", "#7A4C25"),
    ("Pink", "#FF80B0"),
    ("Natural", "#E8E4D8"),
    ("Turquoise", "#00B1B1"),
    ("Lime", "#A8D94B"),
    ("Gold", "#C9A227"),
]

# How many colours a print file can carry. Four is the usual number for
# an AMS or MMU machine, sixteen is the most the big ones take, and one
# is a plain single-spool printer that never changes filament.
MAX_COLOURS = 16
DEFAULT_COLOURS = 4


def palette() -> list:
    """The filament palette, as name and hex."""
    return [{"name": name, "hex": value} for name, value in FILAMENTS]


def hex_to_rgb(code: str) -> tuple:
    """A hex code as three numbers from 0 to 255."""
    text = (code or "#000000").strip().lstrip("#")
    text = (text + "000000")[:6]
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb) -> str:
    """Three numbers as a hex code."""
    return "#%02X%02X%02X" % (max(0, min(255, int(rgb[0]))),
                            max(0, min(255, int(rgb[1]))),
                            max(0, min(255, int(rgb[2]))))


def nearest_filament(rgb, used=None) -> tuple:
    """The closest filament on the shelf, and how close it is.

    The name is only a suggestion, and the distance is given with it,
    because a slot that says "Grey" next to another slot that says
    "Grey" is no use at all to the person standing at the printer. Two
    different colours must never end up with the same name, so the
    names that are already taken are passed in and left out.
    """
    used = used or set()
    best = None
    best_d = None
    for name, code in FILAMENTS:
        if name in used:
            continue
        other = hex_to_rgb(code)
        d = ((other[0] - rgb[0]) ** 2 + (other[1] - rgb[1]) ** 2
             + (other[2] - rgb[2]) ** 2)
        if best_d is None or d < best_d:
            best_d = d
            best = (name, code, d)
    if best is None:
        # Every name on the shelf is taken. There are sixteen names and
        # at most sixteen colours, so this only happens when the shelf
        # is full - and then the closest one is used anyway.
        for name, code in FILAMENTS:
            other = hex_to_rgb(code)
            d = ((other[0] - rgb[0]) ** 2 + (other[1] - rgb[1]) ** 2
                 + (other[2] - rgb[2]) ** 2)
            if best_d is None or d < best_d:
                best_d = d
                best = (name, code, d)
    return best


def _seed_colours(colours, size, masses):
    """Where the reduction starts: the colours furthest apart.

    The darkest, the lightest and the most saturated are always in,
    and the rest are the colours that are furthest from the ones
    already chosen. Weighting by area means a big flat area wins the
    colour it deserves, and a patch of hair does not spend one.
    """
    count = len(colours)
    ordered = sorted(range(count), key=lambda i: (colours[i][0] + colours[i][1]
                                                + colours[i][2]))
    seeds = [colours[ordered[0]], colours[ordered[-1]]]
    if count > 2:
        saturated = max(ordered, key=lambda i: (max(colours[i]) - min(colours[i])))
        seeds.append(colours[saturated])
    while len(seeds) < size:
        spread = []
        for index in range(count):
            colour = colours[index]
            best = None
            for seed in seeds:
                d = ((colour[0] - seed[0]) ** 2 + (colour[1] - seed[1]) ** 2
                     + (colour[2] - seed[2]) ** 2)
                if best is None or d < best:
                    best = d
            spread.append((best * masses[index], index))
        spread.sort(reverse=True)
        seeds.append(colours[spread[0][1]])
    return seeds


def reduce_palette(groups, size=DEFAULT_COLOURS, iterations=12) -> list:
    """The few colours that cover the model, and how much of it each one.

    A group is one colour with the area it covers. The colours that are
    actually on the model are counted first, because a model can have
    hundreds of thousands of faces and only a handful of colours, and
    working on the handful gives the same answer in a fraction of the
    time.

    The names come from the shelf of filaments, and no two of them are
    the same - a person at the printer has to be able to tell the
    spools apart.
    """
    colours = [group["rgb"] for group in groups]
    masses = [max(0.0, float(group.get("area") or 0.0)) for group in groups]
    if not colours:
        return []
    size = max(1, min(int(size), MAX_COLOURS, len(colours)))

    seeds = _seed_colours(colours, size, masses)
    centres = list(seeds)
    for _ in range(iterations):
        sums = [[0.0, 0.0, 0.0, 0.0] for _ in centres]
        for index in range(len(colours)):
            colour = colours[index]
            best = 0
            best_d = None
            for i in range(len(centres)):
                centre = centres[i]
                d = ((colour[0] - centre[0]) ** 2 + (colour[1] - centre[1]) ** 2
                     + (colour[2] - centre[2]) ** 2)
                if best_d is None or d < best_d:
                    best_d = d
                    best = i
            bucket = sums[best]
            weight = masses[index]
            bucket[0] += colour[0] * weight
            bucket[1] += colour[1] * weight
            bucket[2] += colour[2] * weight
            bucket[3] += weight
        for i in range(len(sums)):
            bucket = sums[i]
            if bucket[3]:
                centres[i] = (bucket[0] / bucket[3], bucket[1] / bucket[3],
                              bucket[2] / bucket[3])

    out = []
    used = set()
    for centre in centres:
        rgb = (max(0, min(255, int(round(centre[0])))),
               max(0, min(255, int(round(centre[1])))),
               max(0, min(255, int(round(centre[2])))))
        name, code, distance = nearest_filament(rgb, used)
        used.add(name)
        out.append({"rgb": rgb, "hex": rgb_to_hex(rgb), "name": name,
                    "stock_hex": code, "distance": int(round(distance ** 0.5))})
    return out


def nearest_slot(colour, centres) -> int:
    """Which slot a colour belongs to."""
    best = 0
    best_d = None
    for i in range(len(centres)):
        centre = centres[i]
        d = ((colour[0] - centre[0]) ** 2 + (colour[1] - centre[1]) ** 2
             + (colour[2] - centre[2]) ** 2)
        if best_d is None or d < best_d:
            best_d = d
            best = i
    return best


def group_colours(colours, areas) -> list:
    """The colours that are actually on the model, and what they cover."""
    groups = {}
    for index, colour in enumerate(colours):
        key = (int(colour[0]) & 255, int(colour[1]) & 255, int(colour[2]) & 255)
        group = groups.get(key)
        if group is None:
            group = {"rgb": key, "count": 0, "area": 0.0}
            groups[key] = group
        group["count"] += 1
        if index < len(areas):
            group["area"] += areas[index]
    return list(groups.values())


def _lab(rgb) -> tuple:
    """Display sRGB bytes to CIELAB, with the standard D65 white point."""
    linear = []
    for value in rgb:
        value = value / 255.0
        linear.append(value / 12.92 if value <= 0.04045
                      else ((value + 0.055) / 1.055) ** 2.4)
    r, g, b = linear
    xyz = ((0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047,
           0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
           (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883)
    x, y, z = (v ** (1 / 3) if v > (6 / 29) ** 3
               else v / (3 * (6 / 29) ** 2) + 4 / 29 for v in xyz)
    return (116 * y - 16, 500 * (x - y), 200 * (y - z))


def clean_slots(positions, faces, slots, cell, centres, areas) -> list:
    """Absorb small enclosed islands only into a similar, larger region.

    cell squared is the maximum island area, in the input mesh's units.
    A CIE76 distance above 10 protects a distinct palette colour.
    This is conservative cleanup, not semantic character segmentation.
    """
    if faces <= 0 or cell <= 0:
        return list(slots)
    if len(positions) != faces * 9 or len(slots) != faces or len(areas) != faces:
        raise ValueError("colour cleanup needs one slot and area per triangle")
    if any(slot < 0 or slot >= len(centres) for slot in slots):
        raise ValueError("colour cleanup slot is outside the palette")

    # The renderer stores triangle soup. Match exact endpoints, not nearby
    # positions: rounding would join separate surfaces across a small gap.
    vertices = {}
    edges = {}
    protected = set()
    for face in range(faces):
        corners = []
        for offset in range(face * 9, face * 9 + 9, 3):
            point = tuple(positions[offset:offset + 3])
            if point not in vertices:
                vertices[point] = len(vertices)
            corners.append(vertices[point])
        if len(set(corners)) < 3 or areas[face] <= 0:
            protected.add(face)
            continue
        for a, b in zip(corners, corners[1:] + corners[:1]):
            key = (min(a, b), max(a, b))
            edges.setdefault(key, []).append((face, a < b))

    neighbours = [[] for _ in range(faces)]
    for owners in edges.values():
        if len(owners) == 2 and owners[0][1] != owners[1][1]:
            a, b = owners[0][0], owners[1][0]
            neighbours[a].append(b)
            neighbours[b].append(a)
        else:
            # Leave open, non-manifold and inconsistent-winding regions alone.
            protected.update(face for face, _ in owners)
    del edges, vertices

    labels = array("i", [-1]) * faces
    regions = []
    for face in range(faces):
        if labels[face] != -1:
            continue
        region = len(regions)
        labels[face] = region
        members = []
        stack = [face]
        while stack:
            current = stack.pop()
            members.append(current)
            for other in neighbours[current]:
                if labels[other] == -1 and slots[other] == slots[face]:
                    labels[other] = region
                    stack.append(other)
        regions.append(members)

    masses = [sum(areas[face] for face in members) for members in regions]
    labs = [_lab(rgb) for rgb in centres]
    out = list(slots)
    for region, members in enumerate(regions):
        if not 0 < masses[region] <= cell * cell:
            continue
        if any(face in protected for face in members):
            continue
        border = {labels[other] for face in members for other in neighbours[face]
                  if labels[other] != region}
        if len(border) != 1:
            continue
        surrounding = border.pop()
        if masses[surrounding] <= masses[region]:
            continue
        slot = slots[members[0]]
        winner = slots[regions[surrounding][0]]
        # shortcut: palette contrast cannot identify subtle intentional markings;
        # add an explicit region lock if artists need to protect those too.
        if sum((a - b) ** 2 for a, b in zip(labs[slot], labs[winner])) > 100:
            continue
        for face in members:
            out[face] = winner
    return out


def face_areas(positions, faces) -> list:
    """How much of the surface each face covers."""
    areas = []
    for face in range(faces):
        base = (face * 3) * 3
        ax = positions[base]
        ay = positions[base + 1]
        az = positions[base + 2]
        bx = positions[base + 3]
        by = positions[base + 4]
        bz = positions[base + 5]
        cx = positions[base + 6]
        cy = positions[base + 7]
        cz = positions[base + 8]
        ux = bx - ax
        uy = by - ay
        uz = bz - az
        vx = cx - ax
        vy = cy - ay
        vz = cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        areas.append(0.5 * math.sqrt(nx * nx + ny * ny + nz * nz))
    return areas


def _span(mesh) -> float:
    """How big the model is, across its widest direction."""
    positions = mesh.positions
    if not positions:
        return 1.0
    xs = positions[0::3]
    ys = positions[1::3]
    zs = positions[2::3]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0


def make_plan(positions, faces, colours, size=DEFAULT_COLOURS, noise=1, cell=0.0):
    """The palette, the slot of every face, and what each colour covers."""
    if not colours or faces <= 0:
        return {"palette": [], "slots": [], "faces": 0}
    areas = face_areas(positions, faces)
    groups = group_colours(colours, areas)
    centres = reduce_palette(groups, size)
    if not centres:
        return {"palette": [], "slots": [], "faces": 0}
    # Which slot each of the colours on the model belongs to. This is
    # worked out once per colour and not once per face, because a model
    # can have hundreds of thousands of faces.
    lookup = {}
    rgb_centres = [entry["rgb"] for entry in centres]
    for group in groups:
        lookup[group["rgb"]] = nearest_slot(group["rgb"], rgb_centres)
    slots = [lookup.get((int(c[0]) & 255, int(c[1]) & 255, int(c[2]) & 255), 0)
             for c in colours]
    if cell > 0:
        slots = clean_slots(positions, faces, slots, cell, rgb_centres, areas)
    total = sum(areas) or 1.0
    counts = [0.0] * len(centres)
    for index in range(faces):
        counts[slots[index]] += areas[index]
    for index in range(len(centres)):
        centres[index]["area"] = counts[index]
        centres[index]["share"] = round(100.0 * counts[index] / total, 1)
        centres[index]["index"] = index
    return {"palette": centres, "slots": slots, "faces": faces}


def vectorise(mesh, colours, size=DEFAULT_COLOURS, noise=1) -> dict:
    """Reduce a model to the colours a printer can lay down.

    Noise 1 keeps the palette assignments untouched. Higher settings
    allow larger enclosed, similar-colour islands to be absorbed, without
    crossing gaps or strong colour boundaries.
    """
    faces = mesh.triangles
    if faces <= 0 or not colours:
        return {"palette": [], "slots": [], "faces": 0}
    cell = 0.0
    if noise > 1:
        cell = max(0.4, _span(mesh) / (240.0 - 25.0 * (noise - 1)))
    return make_plan(mesh.positions, faces, colours, size, noise, cell)


def _welded(mesh):
    """The corners of a mesh, joined up the way a print file wants them.

    A mesh that keeps three corners for every triangle is fine to draw,
    but it is not what a print file looks like, and the slicer's own 3MF
    reader will not take one that is bigger than a handful of faces.
    The corners are joined by their place, which is what a shared corner
    means in the file.
    """
    verts = []
    index = {}
    rows = []
    positions = mesh.positions
    for face in range(mesh.triangles):
        row = []
        for corner in range(3):
            i = (face * 3 + corner) * 3
            key = (round(positions[i], 4), round(positions[i + 1], 4),
                   round(positions[i + 2], 4))
            found = index.get(key)
            if found is None:
                found = len(verts)
                index[key] = found
                verts.append(key)
            row.append(found)
        rows.append(row)
    return verts, rows


def write_3mf(pieces, palette, out_path, name="part") -> Path:
    """Write generic per-face 3MF materials, not a validated slicer project.

    Native painted-colour import and machine settings still require a
    separate slicer round-trip check; these tags do not guarantee display
    or physical filament changes.

    A piece is a mesh with flat triangles and the slot of each of those
    triangles. More than one piece is the plate: the copies of a part on
    the bed.
    """
    if not palette:
        raise ValueError("a coloured file needs at least one colour")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    materials = ['<basematerials id="1">']
    for entry in palette:
        code = str(entry.get("hex") or "#808080").lstrip("#").upper()
        if len(code) != 6:
            code = "808080"
        materials.append('<base name="%s" displaycolor="#%sFF"/>'
                         % (escape(str(entry.get("name") or "Colour")), code))
    materials.append("</basematerials>")

    objects = []
    build = []
    for index, piece in enumerate(pieces):
        mesh = piece.get("mesh")
        slots = piece.get("slots") or []
        faces = mesh.triangles
        verts, corners = _welded(mesh)
        rows = []
        for vertex in verts:
            rows.append('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % vertex)
        triangles = []
        for face in range(faces):
            slot = slots[face] if face < len(slots) else 0
            slot = max(0, min(len(palette) - 1, int(slot)))
            row = corners[face]
            triangles.append('<triangle v1="%d" v2="%d" v3="%d" p1="%d" p2="%d" p3="%d"/>'
                             % (row[0], row[1], row[2], slot, slot, slot))
        ident = index + 2
        objects.append('<object id="%d" type="model" pid="1" pindex="0"><mesh>'
                       '<vertices>%s</vertices><triangles>%s</triangles>'
                       '</mesh></object>' % (ident, "".join(rows), "".join(triangles)))
        build.append('<item objectid="%d"/>' % ident)
    model_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<model unit="millimeter" xml:lang="en-US" '
                 ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                 '<metadata name="Application">Malmars Local Print Station</metadata>'
                 '<resources>' + "".join(materials) + "".join(objects)
                 + '</resources><build>' + "".join(build) + '</build></model>')

    content_types = ('<?xml version="1.0" encoding="UTF-8"?>' +
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
                     '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>' +
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>' +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>' +
            '</Relationships>')
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model_xml)
    return out_path


def colour_blob(mesh, colours) -> bytes:
    """The coloured part as the block the page draws.

    Input is one RGB triplet per face. Mode 2 requires one triplet per
    vertex, so repeat each face colour on its three unshared corners.
    """
    positions = mesh.positions
    normals = mesh.normals
    count = len(positions) // 3
    if len(positions) != mesh.triangles * 9 or len(normals) != len(positions):
        raise ValueError("colour preview needs flat triangles and matching normals")
    colours = bytes(colours)
    if len(colours) != mesh.triangles * 3:
        raise ValueError("colour preview needs one RGB triplet per face")
    corners = bytearray()
    for face in range(mesh.triangles):
        corners.extend(colours[face * 3:face * 3 + 3] * 3)
    head = b"JHM1" + struct.pack("<III", count, mesh.triangles, 2)
    return head + positions.tobytes() + normals.tobytes() + corners
