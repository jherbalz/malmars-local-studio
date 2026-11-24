"""Read the slicer's own work back, so the app can show it.

The G-code the slicer writes is not just a list of numbers. Every
extrusion is marked with the part of the print it belongs to - outer
wall, inner wall, gap infill, sparse infill, solid infill, top surface,
bridge, overhang wall. That marking is the answer to "what is this
actually going to print".

This reads those marks, groups the moves by the layer they are on, and
hands back one layer at a time. The app draws it, so the person can
scrub through the layers and see the walls and the infill before
anything is printed.
"""

import re
import threading
from array import array

# The part of the print each kind of move belongs to. These names come
# from the slicer itself, in the slicer's own spelling.
TYPES = [
    "Outer wall",
    "Inner wall",
    "Top surface",
    "Bottom surface",
    "Sparse infill",
    "Gap infill",
    "Internal solid infill",
    "Overhang wall",
    "Bridge",
    "Internal Bridge",
    "Support",
    "Support interface",
    "Custom",
]

# The colour each one is drawn in. The walls are the structure and are
# bright; the infill is the fill and is dimmer; the surfaces that close
# the print are their own colour.
COLOURS = {
    "Outer wall": "#f4b942",
    "Inner wall": "#7fb2ff",
    "Top surface": "#ff7a59",
    "Bottom surface": "#5fd0a8",
    "Sparse infill": "#6f7b8c",
    "Gap infill": "#586273",
    "Internal solid infill": "#9aa6b5",
    "Overhang wall": "#c58cff",
    "Bridge": "#5fd0c8",
    "Internal Bridge": "#4aa39c",
    "Support": "#a06bff",
    "Support interface": "#7a4fd6",
    "Custom": "#8a8f99",
}

_MOVE = re.compile(r"^G1\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?")
_LAYER_Z = re.compile(r"^;Z:(-?[0-9.]+)")
_TYPE = re.compile(r"^;TYPE:(.+)$")
_SETTING = re.compile(r"^;\s*([^=]+?)\s*=\s*(.*)$")

_LOCK = threading.Lock()
_CACHE = {"key": None, "layers": [], "meta": {}, "settings": {}}


def _type_index(name: str) -> int:
    try:
        return TYPES.index(name.strip())
    except ValueError:
        return len(TYPES) - 1


def _key_for(gcode_path):
    try:
        from pathlib import Path
        path = Path(gcode_path)
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except Exception:
        return None


def parse(gcode_path) -> dict:
    """Read the layers out of a G-code file. The result is kept for next time.

    A 28 MB file with 700,000 moves takes about a second to read. It is
    only read again when the file on disk changes.
    """
    key = _key_for(gcode_path)

    with _LOCK:
        if key is not None and _CACHE.get("key") == key:
            return _CACHE

    layers = []
    coords = []
    kinds = []
    settings = {}
    z = 0.0
    x = y = 0.0
    kind = 0
    counts = {}
    x_min = y_min = None
    x_max = y_max = None

    def flush(z_value, coord_list, kind_list):
        if not coord_list:
            return
        layers.append({"z": z_value,
                       "coords": array("f", coord_list),
                       "kinds": bytes(kind_list),
                       "count": len(kind_list)})

    try:
        with open(gcode_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith(";TYPE:"):
                    match = _TYPE.match(line.strip())
                    if match:
                        kind = _type_index(match.group(1))
                    continue
                if line.startswith(";LAYER_CHANGE"):
                    if coords:
                        flush(z, coords, kinds)
                        coords = []
                        kinds = []
                    continue
                if line.startswith(";Z:"):
                    match = _LAYER_Z.match(line.strip())
                    if match:
                        z = float(match.group(1))
                    continue
                if line.startswith(";"):
                    match = _SETTING.match(line.strip())
                    if match and match.group(1) not in settings:
                        settings[match.group(1)] = match.group(2).strip()
                    continue
                if not line.startswith("G1"):
                    continue
                match = _MOVE.match(line)
                if not match:
                    continue
                try:
                    nx = float(match.group(1))
                    ny = float(match.group(2))
                except ValueError:
                    continue
                extruded = match.group(3)
                if extruded is not None:
                    try:
                        if float(extruded) > 0:
                            coords.extend((x, y, nx, ny))
                            kinds.append(kind)
                            counts[kind] = counts.get(kind, 0) + 1
                            if x_min is None or x < x_min:
                                x_min = x
                            if x_max is None or x > x_max:
                                x_max = x
                            if y_min is None or y < y_min:
                                y_min = y
                            if y_max is None or y > y_max:
                                y_max = y
                    except ValueError:
                        pass
                x, y = nx, ny
    except Exception:
        return {"layers": [], "meta": {}, "settings": {}}

    if coords:
        flush(z, coords, kinds)

    meta = {
        "count": len(layers),
        "types": TYPES,
        "colours": COLOURS,
        "counts": {TYPES[k] if k < len(TYPES) else "Custom": v
                   for k, v in counts.items()},
        "total_moves": sum(counts.values()),
    }
    if layers:
        meta["z_first"] = layers[0]["z"]
        meta["z_last"] = layers[-1]["z"]
    if x_min is not None:
        meta["x_min"] = x_min
        meta["x_max"] = x_max
        meta["y_min"] = y_min
        meta["y_max"] = y_max

    result = {"layers": layers, "meta": meta, "settings": settings}
    with _LOCK:
        _CACHE["key"] = key
        _CACHE["layers"] = layers
        _CACHE["meta"] = meta
        _CACHE["settings"] = settings
    return result


def summary(gcode_path) -> dict:
    """What the slicer made, in numbers and in the slicer's own words.

    This is the whole file read once and kept. The app shows the counts
    on the face of it, so the person can see that the walls and the
    infill are really in there before anything is printed.
    """
    data = parse(gcode_path)
    meta = dict(data.get("meta") or {})
    meta["settings"] = settings_of(data.get("settings") or {})
    return meta


# The settings worth showing, in the order a person wants them, with
# the name to call them by. These come out of the slicer's own header.
SHOWN_SETTINGS = [
    ("nozzle_diameter", "Nozzle"),
    ("layer_height", "Layer height"),
    ("wall_loops", "Walls"),
    ("sparse_infill_density", "Infill"),
    ("support_type", "Supports"),
    ("first_layer_bed_temperature", "Bed temperature"),
    ("nozzle_temperature", "Nozzle temperature"),
    ("first_layer_height", "First layer"),
    ("max_z_height", "Model height"),
    ("total_layer_number", "Layers"),
    ("filament used [g]", "Filament used"),
    ("filament cost", "Filament cost"),
]


def settings_of(raw: dict) -> list:
    """The slicer's own settings, ready to show."""
    out = []
    for key, name in SHOWN_SETTINGS:
        if key in raw and raw[key] != "":
            out.append({"name": name, "value": raw[key]})
    return out


def layer(gcode_path, index: int) -> dict:
    """One layer of the print, as the moves the slicer made.

    Each move comes back as [x1, y1, x2, y2, kind] so the browser can
    draw it without being told anything else. One layer at a time is
    the right amount: the whole file is nearly fifteen megabytes of
    coordinates, and nobody needs all of it at once.
    """
    data = parse(gcode_path)
    layers = data.get("layers") or []
    if not layers:
        return {"ok": False, "error": "no layers in that file"}
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        index = 0
    if index >= len(layers):
        index = len(layers) - 1
    item = layers[index]
    flat = item["coords"]
    kinds = item["kinds"]
    segments = []
    for i in range(0, len(flat), 4):
        segments.append([round(flat[i], 3), round(flat[i + 1], 3),
                        round(flat[i + 2], 3), round(flat[i + 3], 3),
                        kinds[i // 4]])
    return {"ok": True, "index": index, "count": len(layers),
            "z": item["z"], "moves": len(segments),
            "types": TYPES, "colours": COLOURS,
            "segments": segments}


class Streamer:
    """Read the G-code while the slicer is still writing it.

    The slicer writes its G-code to a file that grows while it works,
    and the toolpath appears in it layer by layer. Reading that file as
    it grows is how the app can show what the slicer is doing instead
    of waiting for it to finish - which is the whole point of a print
    that is sliced with no window open.
    """

    def __init__(self):
        self.buffer = ""
        self.z = 0.0
        self.kind = 0
        self.segments = []
        self.x = 0.0
        self.y = 0.0
        self.layers = 0
        self.moves = 0
        self.headers = {}

    def feed(self, text):
        """New text from the growing file. The finished layers come back."""
        self.buffer += text
        parts = self.buffer.split("\n")
        # The last piece may be half a line, so it is kept for next time.
        self.buffer = parts.pop()
        finished = []
        for line in parts:
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith(";TYPE:"):
                match = _TYPE.match(line.strip())
                if match:
                    self.kind = _type_index(match.group(1))
                continue
            if line.startswith(";LAYER_CHANGE"):
                if self.segments:
                    finished.append({"z": self.z,
                                     "segments": self.segments,
                                     "moves": len(self.segments)})
                    self.layers += 1
                self.segments = []
                continue
            if line.startswith(";Z:"):
                match = _LAYER_Z.match(line.strip())
                if match:
                    self.z = float(match.group(1))
                continue
            if line.startswith(";"):
                match = _SETTING.match(line.strip())
                if match and match.group(1) not in self.headers:
                    self.headers[match.group(1)] = match.group(2).strip()
                continue
            if not line.startswith("G1"):
                continue
            match = _MOVE.match(line)
            if not match:
                continue
            try:
                nx = float(match.group(1))
                ny = float(match.group(2))
            except ValueError:
                continue
            extruded = match.group(3)
            if extruded is not None:
                try:
                    if float(extruded) > 0:
                        self.segments.append((self.x, self.y, nx, ny,
                                              self.kind))
                        self.moves += 1
                except ValueError:
                    pass
            self.x, self.y = nx, ny
        return finished