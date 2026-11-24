# --- no console windows ------------------------------------------------
# When this app is started by the Studio - which is a windowed program - a
# console child would flash a black window on the desktop. When the app is
# started from a console of its own, the child inherits it and keeps the
# output on screen.
import sys as _sys_mod
import subprocess as _subprocess_mod

_NOWINDOW = getattr(_subprocess_mod, "CREATE_NO_WINDOW", 0)


def _no_console():
    try:
        out = getattr(_sys_mod, "stdout", None)
        return out is None or not out.isatty()
    except Exception:
        return True


if _NOWINDOW and not getattr(_subprocess_mod, "_jherbalz_no_console", False):
    _orig_popen = _subprocess_mod.Popen
    _orig_run = _subprocess_mod.run

    def _popen(*args, **kwargs):
        if _no_console():
            kwargs.setdefault("creationflags", _NOWINDOW)
        return _orig_popen(*args, **kwargs)

    def _run(*args, **kwargs):
        if _no_console():
            kwargs.setdefault("creationflags", _NOWINDOW)
        return _orig_run(*args, **kwargs)

    _subprocess_mod.Popen = _popen
    _subprocess_mod.run = _run
    _subprocess_mod._jherbalz_no_console = True



"""Slice a model into G-code with no window open.

The slicer on this machine is OrcaSlicer. It already ships the profiles for
hundreds of printers - over five hundred machines from forty makers - but it
will only use them from the command line if the profile chain is handed to it
in the shape it expects: a machine file, a process file and a filament file,
each one flattened through its 'inherits' chain and each one stamped with its
own 'type'.

That is what this module does. It finds the profiles the slicer already has,
flattens them, and hands them over - so the person never opens the slicer and
never picks a machine by hand. Adding another printer is nothing at all: the
list is read from the slicer, so every printer it ships is supported the day
the slicer is installed.

The slicer stays a separate, user-installed program. Its licence is not ours
to ship inside this app.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ORCA_CONF = Path(os.environ.get("APPDATA", "")) / "OrcaSlicer" / "OrcaSlicer.conf"
# Where OrcaSlicer may live on this PC. The buyer installs it wherever
# their machine puts programs; the app looks in all the usual places.
ORCA_HOME_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OrcaSlicer",
    Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "OrcaSlicer",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OrcaSlicer",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Orca-Flashforge",
    Path(r"C:\Program Files\OrcaSlicer"),
]
ORCA_PROFILES = ORCA_HOME_CANDIDATES[0] / "resources" / "profiles"

# Flashforge ships their own build of Orca. It is called Flashforge
# Studio, and the engine inside it is Orca-Flashforge.dll - the same
# code as Orca, with Flashforge's own machines in it.
#
# It is the right engine for a Flashforge printer and it is worth
# saying why. Its Adventurer 5M profile carries 123 settings where the
# community build carries 68. Its start sequence waits for the nozzle
# to reach temperature before it purges - M109 - where the community one
# only switches the heater on (M104) and purges straight away at
# whatever temperature the nozzle happens to be. And it slices with no
# window open, the same as the community one.
FLASHFORGE_HOME = (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                   / "Flashforge" / "Orca-Flashforge")
FLASHFORGE_PROFILES = FLASHFORGE_HOME / "resources" / "profiles"
FLASHFORGE_EXE = FLASHFORGE_HOME / "flash studio.exe"

# The printer this house owns. It is only the default - every printer the
# slicer knows about is offered, and this one is chosen when nothing is said.
DEFAULT_PRINTER = "Flashforge Adventurer 5M 0.4 Nozzle"

# The printers people actually look for, shown first in the picker.
FEATURED = [
    "Flashforge Adventurer 5M 0.4 Nozzle",
    "Flashforge Adventurer 5M Pro 0.4 Nozzle",
    "Flashforge AD5X 0.4 nozzle",
    "Bambu Lab X1C 0.4 nozzle",
    "Bambu Lab P1P 0.4 nozzle",
    "Bambu Lab A1 0.4 nozzle",
    "Prusa MK4 0.4 nozzle",
    "Prusa MK4S 0.4 nozzle",
    "Prusa MINI+ 0.4 nozzle",
    "Creality Ender-3 V3 0.4 nozzle",
    "Creality K1 0.4 nozzle",
    "Elegoo Neptune 4 Pro 0.4 nozzle",
    "Anycubic Kobra 2 0.4 nozzle",
]


def _profile_root() -> Path:
    """Where the slicer keeps the profiles it ships with."""
    for home in ORCA_HOME_CANDIDATES:
        candidate = home / "resources" / "profiles"
        if candidate.is_dir():
            return candidate
    return ORCA_PROFILES


def is_flashforge(printer: str = "") -> bool:
    """True when this printer is a Flashforge one."""
    return "flashforge" in str(printer or "").lower()


def _index_for(printer: str = "") -> dict:
    """Every profile that applies to this printer, the right ones winning.

    The community build knows hundreds of machines. The Flashforge build
    knows the Flashforge ones properly. So both are read, and for a
    Flashforge printer the Flashforge copy of a profile wins.
    """
    roots = []
    for home in ORCA_HOME_CANDIDATES:
        candidate = home / "resources" / "profiles"
        if candidate.is_dir():
            roots.append(candidate)
    if is_flashforge(printer) and FLASHFORGE_PROFILES.is_dir():
        roots.append(FLASHFORGE_PROFILES)
    index = {"machine": {}, "process": {}, "filament": {}}
    for root in roots:
        part = _index_profiles(root)
        for kind in index:
            index[kind].update(part[kind])
    return index


def slicer_exe(printer: str = "") -> str:
    """The slicer program that should slice for this printer."""
    if is_flashforge(printer) and FLASHFORGE_EXE.is_file():
        return str(FLASHFORGE_EXE)
    for candidate in (r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
                      r"C:\Program Files\OrcaSlicer\orca-slicer-console.exe"):
        if Path(candidate).is_file():
            return candidate
    return ""


# The settings a person can change, and the names the slicer knows them
# by. These are the same names the slicer uses in its own files, so what
# the app writes is exactly what the slicer reads.
PRINT_OPTIONS = [
    {"id": "scale", "name": "Scale", "unit": "%",
     "min": 10, "max": 200, "step": 5, "def": 100},
    {"id": "copies", "name": "Copies on the bed", "unit": "",
     "min": 1, "max": 20, "step": 1, "def": 1},
    {"id": "layer_height", "name": "Layer height", "unit": "mm",
     "min": 0.08, "max": 0.32, "step": 0.02, "def": 0.2},
    {"id": "wall_loops", "name": "Walls", "unit": "",
     "min": 1, "max": 10, "step": 1, "def": 2},
    {"id": "sparse_infill_density", "name": "Infill", "unit": "%",
     "min": 0, "max": 100, "step": 5, "def": 15},
]


def apply_print_options(flat: dict, options: dict) -> dict:
    """The person's own print settings, folded into the process profile.

    These are the settings that decide what the part actually is - how
    thick the walls are, how much of it is filled in, how big it comes
    out, how many of them fit on the bed, and whether it gets supports.
    Nothing here is a picture of a print. The slicer reads these and
    works out the toolpath from the geometry.
    """
    def number(name):
        raw = options.get(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # The size of the part and the number of copies are NOT set here.
    # The slicer keeps both on the object and will not take them from a
    # profile - and when they are put in a profile it refuses to start
    # at all ("invalid json type for copies"). So the app does that work
    # itself: it scales the mesh and lays the copies out, and the
    # slicer is handed the result.
    # Every value in a profile file is a STRING. The slicer checks
    # the type of each one as it reads the file, and if it finds a
    # number where it expected text it refuses the whole file and
    # carries on with its own defaults - so the settings quietly go
    # back to what the profile said, and nobody is told.
    height = number("layer_height")
    if height is not None:
        flat["layer_height"] = "%.3f" % round(height, 3)
    walls = number("wall_loops")
    if walls is not None:
        flat["wall_loops"] = "%d" % max(1, int(round(walls)))
    infill = number("sparse_infill_density")
    if infill is not None:
        flat["sparse_infill_density"] = "%d%%" % max(0, min(100, int(round(infill))))
    supports = options.get("enable_support")
    if supports is not None:
        flat["enable_support"] = "1" if str(supports).lower() in ("1", "true", "on", "yes") else "0"
    style = options.get("support_type")
    if style:
        flat["support_type"] = str(style)
    return flat


def apply_machine_options(flat: dict, options: dict) -> dict:
    """The person's material settings, folded into the machine profile.

    The filament change is added, never taken away. The Flashforge
    machines carry an empty change command and the flag switched off, so
    a file sliced for one comes out with no filament change in it at
    all. M600 is the command these printers use, and the flag says the
    change is done by hand - which is true, on a printer with one
    extruder.

    These two live on the machine, not on the filament, so this is
    where they go.
    """
    change = options.get("filament_change")
    if change:
        flat["change_filament_gcode"] = "M600"
        flat["manual_filament_change"] = "1"
    # The printer's own bed map. Some machines measure their bed and
    # keep the map in their own firmware; the slicer can be told to
    # leave that much room above the bed so the first layer lands on
    # the high spots instead of the low ones.
    margin = options.get("bed_mesh")
    try:
        margin = float(margin)
    except (TypeError, ValueError):
        margin = 0.0
    if margin > 0:
        flat["adaptive_bed_mesh_margin"] = (int(margin) if margin == int(margin)
                                            else margin)
    return flat


def apply_filament_options(flat: dict, options: dict) -> dict:
    """The person's material settings, folded into the filament profile."""
    colour = options.get("filament_colour")
    if colour:
        flat["filament_colour"] = str(colour)
    temperature = options.get("nozzle_temperature")
    if temperature:
        flat["nozzle_temperature"] = str(temperature)
    return flat


def default_filament(printer: str = DEFAULT_PRINTER) -> str:
    """The material this printer is sliced with unless you choose another."""
    known = {item["id"]: item for item in discover_printers()}
    item = known.get(printer) or known.get(DEFAULT_PRINTER) or {}
    return str(item.get("filament") or "")


def filaments_for(printer: str = DEFAULT_PRINTER) -> list:
    """The materials the slicer has for this printer, with their types."""
    index = _index_for(printer)
    machine = printer or DEFAULT_PRINTER
    family = _family(machine)
    out = []
    for name, data in index["filament"].items():
        compat = data.get("compatible_printers") or []
        if machine not in compat and family not in [_family(c) for c in compat]:
            continue
        flat = _flatten("filament", name, index)
        kind = flat.get("filament_type") or ["PLA"]
        if isinstance(kind, list):
            kind = kind[0] if kind else "PLA"
        out.append({"id": name, "material": str(kind)})
    out.sort(key=lambda item: (item["material"], item["id"]))
    return out


def _index_profiles(root: Path) -> dict:
    """Every bundled profile, keyed by kind and by the name inside it.

    The slicer keeps its profiles under a folder per maker, so the kinds are
    one level down: profiles/Flashforge/machine, profiles/Bambu/machine and
    so on. Every maker is looked at, so a printer from any maker is found the
    same way as one from this house.
    """
    index = {"machine": {}, "process": {}, "filament": {}}
    if not root.is_dir():
        return index
    makers = [root] + [child for child in root.iterdir() if child.is_dir()]
    for maker in makers:
        for kind in index:
            folder = maker / kind
            if not folder.is_dir():
                continue
            for path in folder.rglob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("name"):
                    data.setdefault("_maker", maker.name)
                    index[kind].setdefault(data["name"], data)
    return index


def _family(name: str) -> str:
    """The printer family of a machine name, without the nozzle suffix."""
    return re.sub(r"\s*\d+(\.\d+)?\s*(nozzle|Nozzle)\b", "", name).strip()


def _flatten(kind: str, name: str, index: dict) -> dict:
    """One profile with everything it inherits, already folded in.

    The slicer wants a flat file. A Flashforge machine profile inherits from
    three others before it has a bed size, so this walks the chain and lets the
    closest definition win.
    """
    chain = []
    seen = set()
    current = name
    while current and current in index.get(kind, {}) and current not in seen:
        seen.add(current)
        data = index[kind][current]
        chain.append(data)
        current = data.get("inherits")
    flat = {}
    for data in reversed(chain):
        flat.update(data)
    flat["name"] = name
    flat["from"] = "system"
    flat["type"] = kind
    flat["inherits"] = ""
    flat.pop("_maker", None)
    return flat


def _best_process(processes: list, index: dict) -> str:
    """The process profile to slice with - the standard one when there is one."""
    if not processes:
        return ""
    for wanted in ("0.20mm Standard", "0.20mm", "0.18mm", "0.16mm", "0.12mm"):
        for name in sorted(processes):
            if name.startswith(wanted):
                return name
    return sorted(processes)[0]


def _best_filament(filaments: list, index: dict) -> str:
    """The filament profile to slice with - PLA, and the plainest PLA."""
    if not filaments:
        return ""
    pla = [f for f in sorted(filaments) if "PLA" in f]
    for wanted in ("Generic PLA", "PLA Basic", "PLA @", "PLA"):
        for name in pla:
            if wanted in name and "Silk" not in name and "Galaxy" not in name:
                return name
    if pla:
        return pla[0]
    return sorted(filaments)[0]


def discover_printers() -> list:
    """Every printer the slicer on this machine can slice for.

    A printer is offered when the slicer has a machine profile for it and
    both a process and a filament profile that say they work with it. The
    list is read from the slicer, so it grows by itself as the slicer ships
    more printers.
    """
    index = _index_profiles(_profile_root())
    proc_by = {}
    for pname, pdata in index["process"].items():
        for compat in (pdata.get("compatible_printers") or []):
            proc_by.setdefault(compat, []).append(pname)
    fil_exact = {}
    fil_family = {}
    for fname, fdata in index["filament"].items():
        for compat in (fdata.get("compatible_printers") or []):
            fil_exact.setdefault(compat, []).append(fname)
            fil_family.setdefault(_family(compat), []).append(fname)

    out = []
    for mname, mdata in index["machine"].items():
        if mname.startswith("fdm_"):
            continue
        processes = proc_by.get(mname, [])
        filaments = fil_exact.get(mname) or fil_family.get(_family(mname), [])
        if not processes or not filaments:
            continue
        nozzle = mdata.get("nozzle_diameter") or [""]
        out.append({
            "id": mname,
            "label": mdata.get("printer_model") or mname,
            "maker": mdata.get("_maker", ""),
            "nozzle": nozzle[0] if isinstance(nozzle, list) else str(nozzle),
            "machine": mname,
            "process": _best_process(processes, index),
            "filament": _best_filament(filaments, index),
            "n_process": len(processes),
            "n_filament": len(filaments),
        })

    featured = set(FEATURED)
    out.sort(key=lambda item: (0 if item["id"] in featured else 1,
                               item["maker"], item["label"], item["nozzle"]))
    for position, item in enumerate(out):
        item["featured"] = item["id"] in featured
    return out


def printer_profiles() -> list:
    """The printers this app can slice for, as the UI shows them."""
    return discover_printers()


def _write_profiles(spec: dict, work: Path, options: dict = None) -> list:
    """Write the three flattened profile files the slicer asks for.

    The person's own settings are folded into the process and filament
    profiles before they are written, so the slicer is told what to do
    rather than being asked afterwards.
    """
    index = _index_for(spec.get("machine") or "")
    written = []
    for kind in ("machine", "process", "filament"):
        name = spec.get(kind)
        if not name:
            continue
        flat = _flatten(kind, name, index)
        if not flat:
            continue
        if kind == "machine":
            apply_machine_options(flat, options or {})
        elif kind == "process":
            apply_print_options(flat, options or {})
        elif kind == "filament":
            apply_filament_options(flat, options or {})
        path = work / (kind + ".json")
        path.write_text(json.dumps(flat, indent=1), encoding="utf-8")
        written.append(path)
    return written


_BED_CACHE = {"key": None, "beds": []}


def _profile_roots() -> list:
    """Every folder the slicer keeps its profiles in."""
    roots = []
    for home in ORCA_HOME_CANDIDATES:
        candidate = home / "resources" / "profiles"
        if candidate.is_dir():
            roots.append(candidate)
    if FLASHFORGE_PROFILES.is_dir():
        roots.append(FLASHFORGE_PROFILES)
    return roots


def bed_areas() -> list:
    """Every bed size the slicer knows about, smallest first.

    This is what the app shows in the preview: the part on a 100 mm
    bed and the same part on a 300 mm bed look very different, and
    that is the thing a person needs to see before anything is
    printed.
    """
    key = "raw"
    if _BED_CACHE["key"] == key:
        return _BED_CACHE["beds"]
    found = {}
    for root in _profile_roots():
        for path in root.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            raw = data.get("printable_area")
            if not isinstance(raw, list) or len(raw) < 4:
                continue
            corners = []
            for point in raw:
                try:
                    x_text, y_text = str(point).lower().split("x", 1)
                    corners.append((float(x_text), float(y_text)))
                except (ValueError, TypeError):
                    corners = []
                    break
            if not corners:
                continue
            area = (min(p[0] for p in corners), max(p[0] for p in corners),
                    min(p[1] for p in corners), max(p[1] for p in corners))
            if area[1] - area[0] <= 0 or area[3] - area[2] <= 0:
                continue
            mark = (round(area[0], 1), round(area[1], 1),
                    round(area[2], 1), round(area[3], 1))
            entry = found.get(mark)
            if entry is None:
                entry = {"area": list(area), "w": area[1] - area[0],
                         "d": area[3] - area[2], "printers": []}
                found[mark] = entry
            label = (data.get("printer_model") or path.stem).replace("_", " ")
            if label not in entry["printers"] and len(entry["printers"]) < 6:
                entry["printers"].append(label)
    beds = sorted(found.values(), key=lambda item: (item["w"] * item["d"], item["w"]))
    for item in beds:
        item["printers"] = item["printers"][:4]
        item["name"] = "%d x %d mm" % (round(item["w"]), round(item["d"]))
    _BED_CACHE["key"] = key
    _BED_CACHE["beds"] = beds
    return beds


def machine_recipe(printer: str = DEFAULT_PRINTER) -> dict:
    """Everything the app needs to know about how this printer works.

    Three things, all of them from the same machine profile the slicer is
    given, so the app and the slicer can never disagree:

    - the start and end G-code. Every printer purges in its own way and in
      its own place - for the Flashforge that is the chute in front of the
      bed, not the bed. A prime line written by hand gets that wrong.
    - the printable area. This is the part that matters most and is
      easiest to get wrong: the Adventurer 5M puts the origin in the
      CENTRE of the bed, so the bed runs from -110 to 110 on both axes.
      A prime line drawn from X20 to X190 drives the head off the sheet
      and jams it against the frame, and the printer freezes mid-move
      with nothing coming out.
    """
    index = _index_profiles(_profile_root())
    known = {item["id"]: item for item in discover_printers()}
    item = known.get(printer or DEFAULT_PRINTER) or known.get(DEFAULT_PRINTER) or {}
    machine = item.get("machine") or ""
    flat = _flatten("machine", machine, index) if machine else {}
    if not flat:
        return {"start": "", "end": "", "area": None, "height": 0.0}
    area = None
    raw = flat.get("printable_area")
    if isinstance(raw, list) and len(raw) >= 4:
        corners = []
        for point in raw:
            try:
                x_text, y_text = str(point).lower().split("x", 1)
                corners.append((float(x_text), float(y_text)))
            except (ValueError, TypeError):
                corners = []
                break
        if corners:
            area = (min(p[0] for p in corners), max(p[0] for p in corners),
                    min(p[1] for p in corners), max(p[1] for p in corners))
    height = 0.0
    try:
        height = float(flat.get("printable_height") or 0.0)
    except (TypeError, ValueError):
        height = 0.0
    return {"start": str(flat.get("machine_start_gcode") or ""),
            "end": str(flat.get("machine_end_gcode") or ""),
            "area": area, "height": height}


def slice_headless(model: Path, out_dir: Path, printer: str = DEFAULT_PRINTER,
                   timeout: int = 900, options: dict = None,
                   watch=None) -> Path:
    """Slice a model into G-code with no window open.

    Returns the G-code path, or an empty path when the slicer did not write
    one. Nothing here needs the person to have touched the slicer once.

    The engine is the printer's own maker's build when it has one - the
    Flashforge build for a Flashforge printer - because it knows that
    machine properly. Everything else uses the community build.
    """
    options = options or {}
    machine = printer or DEFAULT_PRINTER
    process = ""
    filament = ""

    known = {item["id"]: item for item in discover_printers()}
    if machine in known:
        item = known[machine]
        process = item["process"]
        filament = item["filament"]
    else:
        # Not a known printer - fall back to the profiles of this house.
        machine = DEFAULT_PRINTER
        fallback = known.get(machine, {})
        process = fallback.get("process", "")
        filament = fallback.get("filament", "")

    # The person can choose the material. It has to be one the slicer
    # has for this printer, or the temperatures will not match it.
    wanted_filament = str(options.get("filament") or "").strip()
    if wanted_filament:
        known_filaments = {item["id"] for item in filaments_for(machine)}
        if wanted_filament in known_filaments:
            filament = wanted_filament

    spec = {"machine": machine, "process": process, "filament": filament}

    slicer = slicer_exe(machine)
    if not slicer:
        return Path()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".slice"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    try:
        profiles = _write_profiles(spec, work, options)
        if len(profiles) < 3:
            return Path()
        model_copy = work / Path(model).name
        shutil.copy2(model, model_copy)
        gcode_dir = work / "gcode"
        argv = [slicer]
        # The machine and the process come in as settings; the filament has
        # its own flag. Without it the slicer takes the filament's name but
        # not its weight, and every print reports zero grams and zero cost.
        for profile in profiles:
            if profile.stem == "filament":
                argv += ["--load-filaments", str(profile)]
            else:
                argv += ["--load-settings", str(profile)]
        argv += [model_copy.name, "--slice", "0", "--outputdir", str(gcode_dir)]
        try:
            process = subprocess.Popen(argv, cwd=str(work),
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
        except OSError:
            return Path()

        # Whatever the slicer says while it works is passed on as it
        # arrives, so the person sees the slicer's own words.
        def pump():
            try:
                for raw in iter(process.stdout.readline, b""):
                    if watch:
                        watch("line", raw.decode("utf-8", "replace"))
            except Exception:
                pass

        reader = threading.Thread(target=pump)
        reader.daemon = True
        reader.start()

        # The G-code is written to a file that grows while the slicer
        # works. Watching that file is how the app can show the print
        # being made instead of waiting for it to be finished.
        deadline = time.time() + timeout
        seen = {}
        while process.poll() is None:
            if time.time() > deadline:
                try:
                    process.kill()
                except OSError:
                    pass
                return Path()
            time.sleep(0.2)
            if not watch:
                continue
            try:
                growing = list(gcode_dir.glob("*.gcode.tmp"))
            except OSError:
                growing = []
            for path in growing:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if seen.get(path) != size:
                    seen[path] = size
                    watch("gcode", (path, size))
        try:
            process.wait(timeout=10)
        except Exception:
            pass
        produced = sorted(gcode_dir.glob("*.gcode")) if gcode_dir.is_dir() else []
        if not produced:
            return Path()
        newest = max(produced, key=lambda p: p.stat().st_mtime)
        target = out_dir / ("jherbalz_" + newest.name)
        shutil.copy2(newest, target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slicers
# ---------------------------------------------------------------------------

# The slicers a print file can be handed to. The Flashforge Studio is the
# one on this machine - it is an Orca fork with the Flashforge machine and
# filament profiles in it, but this build has no command line, so it is a
# hand-off and not a headless slice.
SLICER_APPS = [
    {"id": "flashforge", "name": "Flashforge Studio",
     "paths": [r"C:\Program Files\Flashforge\Orca-Flashforge\flash studio.exe"],
     "exe": "flash studio.exe", "headless": False},
    {"id": "orcaslicer", "name": "OrcaSlicer",
     "paths": [r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
               r"C:\Program Files\OrcaSlicer\orca-slicer-console.exe"],
     "exe": "orca-slicer.exe", "headless": True},
    {"id": "bambu", "name": "Bambu Studio",
     "paths": [r"C:\Program Files\Bambu Studio\bambu-studio.exe"],
     "exe": "bambu-studio.exe", "headless": False},
    {"id": "prusaslicer", "name": "PrusaSlicer",
     "paths": [r"C:\Program Files\PrusaSlicer\prusa-slicer.exe"],
     "exe": "prusa-slicer.exe", "headless": True},
    {"id": "cura", "name": "UltiMaker Cura",
     "paths": [r"C:\Program Files\UltiMaker Cura\UltiMaker Cura.exe"],
     "exe": "cura.exe", "headless": False},
]


def detect_slicers() -> list:
    """Which slicers are on this machine.

    Only the known install folders are looked in. A deep scan of Program
    Files takes minutes and finds nothing that matters.
    """
    out = []
    for app in SLICER_APPS:
        found = ""
        for candidate in app["paths"]:
            if Path(candidate).is_file():
                found = candidate
                break
        if not found:
            for base in (Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")):
                if not base.is_dir():
                    continue
                try:
                    children = list(base.iterdir())
                except OSError:
                    continue
                for child in children:
                    if not child.is_dir():
                        continue
                    if app["id"] not in child.name.lower().replace(" ", ""):
                        continue
                    for exe in child.glob(app["exe"]):
                        found = str(exe)
                        break
                    if found:
                        break
                if found:
                    break
        out.append({"id": app["id"], "name": app["name"], "path": found,
                    "present": bool(found), "headless": app["headless"]})
    return out


def open_in_slicer(model: Path, printer: str = DEFAULT_PRINTER,
                   slicer_path: str = "") -> bool:
    """Open the model for visual review without replacing custom presets.

    Choose/check the actual machine and filament in the slicer. Flashforge
    printers prefer Flashforge Studio, falling back to Orca when unavailable.
    """
    exe = slicer_path or ""
    if not exe:
        detected = {app["id"]: app["path"] for app in detect_slicers()
                    if app.get("present") and app.get("path")}
        preference = (("flashforge", "orcaslicer") if is_flashforge(printer)
                      else ("orcaslicer",))
        exe = next((detected[name] for name in preference if detected.get(name)), "")
    model = Path(model).resolve()
    if not exe or not Path(exe).is_file() or not model.is_file():
        return False

    try:
        subprocess.Popen([exe, str(model)], cwd=str(model.parent))
        return True
    except OSError:
        return False


def gcode_facts(path: Path) -> dict:
    """What the slicer worked out about this print.

    The slicer writes its own account of the print into the G-code: how much
    filament it will use, how long it will take, how many layers. The
    filament weight is what a person wants to know before the money and the
    time are spent, so it is read back out and shown.
    """
    facts = {"grams": 0.0, "cm3": 0.0, "time_text": "", "seconds": 0,
             "layers": 0, "cost": 0.0, "filament": ""}
    if not path or not Path(path).is_file():
        return facts
    try:
        with Path(path).open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if line.startswith(';') and ('=' in line or ':' in line):
                    text = line[1:]
                    if '=' in text:
                        key, _, value = text.partition('=')
                    else:
                        key, _, value = text.partition(':')
                    key = key.strip().lower()
                    value = value.strip()
                    if key == 'filament used [g]':
                        try:
                            facts['grams'] = float(value)
                        except ValueError:
                            pass
                    elif key == 'filament used [cm3]':
                        try:
                            facts['cm3'] = float(value)
                        except ValueError:
                            pass
                    elif key == 'total filament used [g]':
                        try:
                            facts['grams'] = max(facts['grams'], float(value))
                        except ValueError:
                            pass
                    elif key == 'total filament cost':
                        try:
                            facts['cost'] = float(value)
                        except ValueError:
                            pass
                    elif key == 'default_filament_profile':
                        facts['filament'] = value.strip('"')
                    elif key == 'estimated printing time (normal mode)':
                        facts['time_text'] = value
                        facts['seconds'] = _seconds_from(value)
                    elif key == 'total layer number':
                        try:
                            facts['layers'] = int(float(value))
                        except ValueError:
                            pass
    except OSError:
        return facts
    return facts


def _seconds_from(text: str) -> int:
    """'10h 18m 56s' as a number of seconds."""
    total = 0
    for value, unit in re.findall(r'(\d+)\s*([hms])', text or ''):
        number = int(value)
        if unit == 'h':
            total += number * 3600
        elif unit == 'm':
            total += number * 60
        else:
            total += number
    return total


def gcode_file_for(run_dir: Path) -> Path:
    """The newest sliced file in a run. A printer wants G-code, not a 3MF."""
    best = None
    best_at = 0.0
    for path in run_dir.rglob("*.gcode"):
        try:
            if not path.is_file():
                continue
            at = path.stat().st_mtime
        except OSError:
            continue
        if at > best_at:
            best_at = at
            best = path
    return best or Path()


def print_file_for(run_dir: Path) -> Path:  # kept for the world gen app
    """The newest print file in a run - the 3MF if there is one."""
    best = None
    best_at = 0.0
    for path in run_dir.rglob("*"):
        if path.suffix.lower() not in (".3mf", ".stl"):
            continue
        try:
            if not path.is_file():
                continue
            at = path.stat().st_mtime
        except OSError:
            continue
        # A 3MF is the better hand-off, so it wins over an STL.
        rank = at + (1000000 if path.suffix.lower() == ".3mf" else 0)
        if rank > best_at:
            best_at = rank
            best = path
    return best or Path()
