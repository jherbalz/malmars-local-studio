"""Getting the bed ready before anything is printed.

A print that does not stick is not a bad model and not a bad printer. It is
a bed that has not been cleaned, a filament that has been left open until it
went soft and wet, or a surface with nothing on it for the plastic to hold.

So there are two halves to this: what a person has to do with their hands,
and what the machine can do for itself. The app owns the second half - it
heats the bed, levels it, and lays down a prime line along the front edge
so the person can see at once whether the plastic is going to hold.
"""

from __future__ import annotations

import re
from pathlib import Path

# How each material has to be treated before it will stick to a bed.
MATERIALS = {
    "PLA": {
        "dry_c": 45, "dry_hours": 6,
        "surface": "clean PEI sheet, or a glue stick",
        "note": "Plain PLA is fine on a clean textured sheet. It does not "
                "need glue, and glue makes it harder to remove.",
    },
    "PETG": {
        "dry_c": 60, "dry_hours": 8,
        "surface": "glue stick, or a clean PEI sheet",
        "note": "PETG does not stick to bare smooth PEI. A thin glue layer "
                "is the difference between a print and a pile of plastic.",
    },
    "ABS": {
        "dry_c": 70, "dry_hours": 6,
        "surface": "glue stick on the sheet",
        "note": "ABS shrinks as it cools and pulls away from the bed. Glue "
                "and a warm room are both part of the job.",
    },
    "ASA": {
        "dry_c": 70, "dry_hours": 6,
        "surface": "glue stick on the sheet",
        "note": "ASA behaves like ABS outdoors. It needs glue and it needs "
                "the room warm, or the corners lift.",
    },
    "TPU": {
        "dry_c": 45, "dry_hours": 4,
        "surface": "clean PEI sheet, no glue",
        "note": "TPU sticks to almost anything. It does not need glue, and "
                "glue turns the bottom of the print into a nightmare.",
    },
}

DEFAULT_MATERIAL = "PLA"

# The checks a person has to pass before the model is allowed near the bed.
CHECKS = [
    {
        "id": "bed_clean",
        "title": "The bed is clean",
        "detail": "Wipe the whole sheet with 90% isopropyl alcohol and a "
                  "lint-free cloth. Scrape off any burnt plastic first with "
                  "a plastic scraper, or it will print into the next one.",
    },
    {
        "id": "surface",
        "title": "There is something for it to hold on to",
        "detail": "A clean textured or smooth PEI sheet, a sheet of matte "
                  "masking tape, or a thin layer of glue stick. Bare glass "
                  "and bare smooth plastic will not hold PLA.",
    },
    {
        "id": "filament_dry",
        "title": "The filament is dry",
        "detail": "Filament that has been open for weeks has taken water out "
                  "of the air. It strings, it weakens, and it will not stick. "
                  "Dry it in a filament dryer or a warm oven before the print.",
    },
    {
        "id": "nozzle_clear",
        "title": "The nozzle is clear",
        "detail": "A blocked nozzle drops nothing for the first few layers "
                  "and the print then lifts. The prime line below is the "
                  "test - if the line does not come out clean, the nozzle "
                  "needs clearing first.",
    },
]


def material_for(name: str) -> dict:
    """The care instructions for a filament name, matched loosely."""
    text = (name or "").upper()
    for key in MATERIALS:
        if key in text:
            item = dict(MATERIALS[key])
            item["material"] = key
            return item
    item = dict(MATERIALS[DEFAULT_MATERIAL])
    item["material"] = DEFAULT_MATERIAL
    return item


def temperatures_from_gcode(path) -> tuple:
    """The nozzle and bed temperatures the slicer chose for this model.

    The prime line has to run at the same heat as the real print, or the
    test proves nothing.
    """
    nozzle = 0.0
    bed = 0.0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            # The file is read until the first layer, not for a fixed
            # number of lines. The Flashforge build of the slicer writes
            # a very long settings block before the commands, and the
            # temperatures are in the commands - so a line limit reads
            # past the temperatures and the prime line then runs at the
            # wrong bed temperature, which is the same kind of stall as
            # before, only smaller.
            for line in handle:
                if line.startswith(";LAYER_CHANGE"):
                    break
                # The first-layer temperatures the slicer chose. The bed
                # is set with M190 or M140, the nozzle with M104 or M109,
                # and the numbers can be decimals.
                #
                # These have to be the temperatures of the model itself.
                # If the prime runs at a different heat from the print,
                # the printer has to bring the bed from one to the other
                # before it will start, and a bed that has to cool from
                # 60 C to 35 C takes twenty minutes and looks exactly
                # like a printer that has hung.
                match = re.match(r"^M104\s+S([0-9.]+)", line)
                if match and nozzle <= 0:
                    nozzle = float(match.group(1))
                match = re.match(r"^M109\s+S([0-9.]+)", line)
                if match and nozzle <= 0:
                    nozzle = float(match.group(1))
                match = re.match(r"^M190\s+S([0-9.]+)", line)
                if match and bed <= 0:
                    bed = float(match.group(1))
                match = re.match(r"^M140\s+S([0-9.]+)", line)
                if match and bed <= 0:
                    bed = float(match.group(1))
                if nozzle > 0 and bed > 0:
                    break
    except Exception:
        pass
    if nozzle <= 0:
        nozzle = 220.0
    if bed <= 0:
        bed = 60.0
    return nozzle, bed


def model_bounds(gcode_path) -> tuple:
    """Where the model will actually be printed, in the printer's own numbers.

    The G-code is read after the first layer change, so the printer's own
    purge and wipe moves at the start are not counted. The answer is the
    footprint of the model: the left, right, front and back of it.
    """
    left = right = front = back = None
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="replace") as handle:
            started = False
            for line in handle:
                if not started:
                    if line.startswith(";LAYER_CHANGE"):
                        started = True
                    continue
                if not line.startswith("G1"):
                    continue
                match = re.match(r"^G1\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)", line)
                if not match:
                    continue
                try:
                    x = float(match.group(1))
                    y = float(match.group(2))
                except ValueError:
                    continue
                left = x if left is None else min(left, x)
                right = x if right is None else max(right, x)
                front = y if front is None else min(front, y)
                back = y if back is None else max(back, y)
    except Exception:
        return None
    if left is None:
        return None
    return (left, right, front, back)


def _line_place(bounds, area) -> tuple:
    """Where the two adhesion lines go.

    They are laid just in front of the model, on the same part of the bed
    the model will use, and they are clamped inside the printable area so
    the head can never be sent off the sheet. On a printer whose origin is
    in the middle of the bed - the Adventurer 5M - the bed runs from -110
    to 110, and a line drawn past that jams the head against the frame.
    """
    x_lo, x_hi = -100.0, 100.0
    y_lo, y_hi = -100.0, 100.0
    if area:
        x_lo, x_hi, y_lo, y_hi = area
        # Keep well inside the edges, whatever the printer says.
        x_lo += 6.0
        x_hi -= 6.0
        y_lo += 6.0
        y_hi -= 6.0
    if bounds:
        left, right, front, back = bounds
        left = max(left, x_lo)
        right = min(right, x_hi)
        if right - left < 20.0:
            middle = (max(left, x_lo) + min(right, x_hi)) / 2.0
            left = middle - 15.0
            right = middle + 15.0
        first = max(front - 6.0, y_lo)
        second = max(front - 12.0, y_lo)
    else:
        left, right = x_lo, x_hi
        first = max(y_lo, -20.0)
        second = first + 8.0
    if second <= first:
        second = first + 6.0
    return (left, right, first, second)


def prime_gcode(nozzle_c: float = 210.0, bed_c: float = 60.0,
                start_gcode: str = "", end_gcode: str = "",
                bounds=None, area=None,
                name: str = "jherbalz_bed_prime.gcode") -> str:
    """A short job that proves the plastic will stick, before the model.

    It heats the bed and the nozzle, homes the machine, and lays two lines
    along the front edge of the bed. Nothing else. If those lines hold,
    the model will hold. If they do not, the person finds out now instead
    of two hours later.
    """
    nozzle = max(0.0, float(nozzle_c))
    bed = max(0.0, float(bed_c))
    # A 0.4 mm nozzle laying 0.2 mm high, 1.75 mm filament.
    layer = 0.2 * 0.4
    filament_area = 3.14159265 * (1.75 / 2.0) ** 2
    per_mm = layer / filament_area
    length = 170.0
    amount = round(length * per_mm, 2)

    lines = [
        "; Malmars Local Print Station - bed prime",
        "; This is the adhesion test. If these lines do not stick, the",
        "; model will not stick either.",
        "; nozzle %.0f C, bed %.0f C" % (nozzle, bed),
        "; estimated printing time (normal mode) = 0m 30s",
        "",
    ]

    # The printer's own start G-code, taken from its machine profile. This
    # is what heats it, homes it and purges it - in the right place, with
    # the right commands, in the G-code dialect it actually speaks. The
    # 5M runs Klipper, and a hand-written prime gets that wrong.
    start = (start_gcode or "").strip()
    if start:
        start = start.replace("[bed_temperature_initial_layer_single]", "%.0f" % bed)
        start = start.replace("[nozzle_temperature_initial_layer]", "%.0f" % nozzle)
        start = start.replace("[bed_temperature_initial_layer]", "%.0f" % bed)
        start = start.replace("[nozzle_temperature]", "%.0f" % nozzle)
        lines.append("; the printer's own start sequence")
        for line in start.splitlines():
            lines.append(line)
            # M104 only switches the heater on; it does not wait. The
            # purge that follows has to happen with the nozzle at full
            # heat, or it does not clear anything and the print starts
            # with a blocked nozzle. M109 waits.
            if re.match(r"^\s*M104\s+S[0-9.]+", line):
                lines.append("M109 S%.0f" % nozzle)
        lines.append("")
    else:
        lines += ["M140 S%.0f" % bed, "M104 S%.0f" % nozzle,
                  "M190 S%.0f" % bed, "M109 S%.0f" % nozzle,
                  "G90", "M83", "G28", "G1 Z5 F6000", ""]

    # The two adhesion lines, laid on the bed, in front of the model and
    # inside the printable area. The filament is relative, which is what
    # the printer's own profile uses.
    left, right, front, back = _line_place(bounds, area)
    lines += [
        "; the adhesion lines, on the bed in front of the model",
        "G90",
        "M83",
        "G92 E0",
        "G1 X%.2f Y%.2f F6000" % (left, front),
        "G1 Z0.2 F1200",
        "G1 X%.2f Y%.2f E%.2f F1200" % (right, front, amount),
        "G1 X%.2f Y%.2f E%.2f F1200" % (left, back, amount),
        "",
    ]

    # And the printer's own end sequence, so it parks and cools the way it
    # expects to.
    end = (end_gcode or "").strip()
    if end:
        lines.append("; the printer's own end sequence")
        for line in end.splitlines():
            # The bed stays at the print temperature after the prime. If
            # it is switched off here, the model that follows has to
            # bring the bed back up - and if it wants a cooler bed, it
            # has to wait for the bed to cool down, which looks like a
            # printer that has hung. The nozzle is switched off, because
            # a nozzle left standing at 220 C is a fire risk.
            if re.match(r"^\s*M140\s+S0\b", line):
                continue
            lines.append(line)
    else:
        lines += ["G1 E-3 F3600", "G0 X50 Y50 F30000", "M104 S0"]
    lines += ["M84", ""]
    return "\n".join(lines)


def write_prime(out_dir: Path, nozzle_c: float, bed_c: float,
                  start_gcode: str = "", end_gcode: str = "",
                  bounds=None, area=None) -> Path:
    """Write the prime job into the work folder and say where it went."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "jherbalz_bed_prime.gcode"
    target.write_text(prime_gcode(nozzle_c, bed_c, start_gcode, end_gcode,
                                  bounds, area), encoding="utf-8")
    return target
