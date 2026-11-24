"""MALMARS LOCAL PRINT STATION.

Drop a model in and open it in the slicer already on this PC. Review and
adjust the real model there, then explicitly choose whether to print.

Python 3.9 or newer. Nothing else to install except a slicer, installed
separately. There is no account, cloud, telemetry or internet upload. Local
app handoffs stay on this PC; printer traffic stays on the local network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import wgbed
import wglicence
import wgcolour
import wgmesh
import wgprinter
import wglayers
from slicers import (DEFAULT_PRINTER, PRINT_OPTIONS, default_filament,
                     detect_slicers, filaments_for, gcode_facts,
                     machine_recipe, open_in_slicer, printer_profiles,
                     slice_headless)
from wglocations import bundle_dir, data_dir
APPS_ROOT = ROOT.parent.parent
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))
try:
    from studio_gate import require_product as _require_studio_product
except Exception:  # a standalone build carries no Studio gate
    _require_studio_product = None


def _studio_root_found():
    """True when a complete Malmars Local Studio tree sits above this app."""
    if os.environ.get("JL_STUDIO_ROOT"):
        return True
    for parent in Path(__file__).resolve().parents:
        if ((parent / "license_config.json").is_file()
                and (parent / "studio_policy.py").is_file()):
            return True
    return False


def _studio_gate(product_id):
    """Ask the Studio gate whether this app may start.

    The gate belongs to the Studio, so the standalone free beta executable
    has no Studio configuration to check against. There is nothing to
    confirm and nothing to refuse there, so the check is skipped instead of
    blocking the app.
    """
    if _require_studio_product is None:
        return
    try:
        _require_studio_product(product_id, __file__)
    except RuntimeError:
        if _studio_root_found():
            raise
        return


def _in_studio():
    """True when the Studio launcher started this app.

    A standalone install - the free beta executable - has no launcher and
    no Studio, so it must not offer Studio pages, Studio hardware checks
    or Studio setup, even on a PC that happens to have the full Studio
    installed and running. The launcher tells the app which product it is;
    a standalone run never gets that answer.
    """
    return bool(os.environ.get("JL_STUDIO_PRODUCT")) and _studio_root_found()
# The app's own files live in the bundle (read only). Everything the app
# writes - models, slices, the licence, the printer's access code - lives
# next to the app, or in the user's own app data if that cannot be written.
BUNDLE = bundle_dir()
DATA = data_dir()
WEB = BUNDLE / "web"


def _app_page_html():
    """The app page, without the Studio link when there is no Studio.

    Returns None when the page file is missing.
    """
    target = (WEB / "index.html").resolve()
    try:
        data = target.read_bytes().decode("utf-8")
    except OSError:
        return None
    if not _in_studio():
        data = re.sub(r'<a [^>]*data-hardware-link[^>]*>.*?</a>', '', data,
                      flags=re.S)
    return data
MODELS = DATA / "models"
WORK = DATA / "work"
# Optional local estimates only; never automatically selected for printing.
GCODE = DATA / "gcode"
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



PORT = int(os.environ.get("PRINT_STATION_PORT", "8779"))
HOST = "127.0.0.1"
# The build number the app reports when it is asked. It is the same
# number that is stamped into the EXE, so a screenshot of the health page
# and the file sitting on the desk can never disagree.
VERSION = "1.5.5"

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MODEL_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_SOURCE_UPLOAD_BYTES = 32 * 1024 * 1024

MODEL_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".glb")

# What was sliced last, so the Send button knows what it is sending.
STATE = {"gcode": "", "facts": {}, "name": "", "printer": "", "model": "",
         "source": "", "bed_ready": False, "bed_prime": "",
         "bed_prime_started": False, "bed_checks": {}}
LOCK = threading.Lock()


def _address(settings: dict) -> str:
    return settings.get("address") or settings.get("host") or ""


def _read_printer(address: str, serial: str) -> dict:
    """Ask one printer what it is doing. Three quick questions."""
    entry = {"address": address, "serial": serial}
    try:
        with wgprinter.Printer(address) as link:
            entry["info"] = link.info()
            entry["status"] = link.status()
            entry["temps"] = link.temperatures()
    except Exception as exc:
        entry["error"] = str(exc)[:200]
    return entry


def printer_snapshot() -> dict:
    """The printer on this network, and what it says about itself.

    The address found last time is remembered, so the usual answer does not
    pay for a fresh search. A search is only run when the printer we know
    about is not answering, or when we have never seen one.
    """
    settings = wgprinter.load_settings()
    address = _address(settings)
    serial = settings.get("serial") or ""
    found = []

    if address:
        entry = _read_printer(address, serial)
        if entry.get("error"):
            # The printer we knew about is not answering. Look for another.
            found = wgprinter.find_printers(1.5)
            if found and found[0]["address"] != address:
                address = found[0]["address"]
                serial = serial or found[0]["serial"]
                entry = _read_printer(address, serial)
    else:
        found = wgprinter.find_printers(1.5)
        if found:
            address = found[0]["address"]
            serial = serial or found[0]["serial"]
            entry = _read_printer(address, serial)
        else:
            entry = {}

    if address and not entry.get("error"):
        # The printer's own idea of what it is doing. The TCP channel only
        # says BUSY, and that cannot tell a real print from a job that was
        # cancelled and never cleared.
        entry["http_state"] = wgprinter.printer_status_detail(
            address, serial, settings.get("check_code") or "")
        # And whether it is sitting at its own screen waiting for the
        # filament to be confirmed, which looks like a print that has
        # stalled but is really the printer asking a question.
        entry["waiting_for_filament"] = printer_waiting_for_filament(address)
    out = {"ok": True, "printers": found, "printer": entry,
           "has_code": bool(settings.get("check_code")),
           "last_prompt": STATE.get("last_prompt") or ""}
    if address and not entry.get("error"):
        # Remember it, so the next question does not pay for a search.
        wgprinter.save_settings({"address": address, "serial": serial})
    return out


def wait_for_the_nozzle(gcode: Path) -> str:
    """Make the printer wait for its nozzle before it purges.

    The machine profile the slicer uses switches the heater on with M104
    and then purges straight away. M104 does not wait. On a nozzle that
    was turned off a moment ago, that purge happens at 120 C, where it
    does not clear anything and can leave the print starting with a
    blocked nozzle.

    One extra line - M109, which does wait - makes the print begin the
    way it should. Nothing else in the file is touched.
    """
    try:
        data = gcode.read_bytes()
    except OSError:
        return ""
    head, sep, tail = data.partition(b";LAYER_CHANGE")
    match = re.search(rb"(?m)^([ \t]*M104[ \t]+S([0-9.]+)[ \t]*)$", head)
    if not match:
        return ""
    temperature = match.group(2).decode("ascii", "replace")
    line = ("M109 S%s" % temperature).encode("ascii")
    if match.group(1):
        line = b" " + line
    head = head[:match.end(1)] + b"\n" + line + head[match.end(1):]
    try:
        gcode.write_bytes(head + sep + tail)
    except OSError:
        return ""
    return temperature


# -- the slice job ---------------------------------------------------------
#
# The slicer takes seconds to minutes, and a person has to be able to
# watch it. So the slice runs in the background and the page asks how it
# is going. The live view is the slicer's own G-code: it writes that file
# while it works, and the app reads it as it grows, so the print is seen
# being made - which is the thing a slice with no window open never
# showed.

SLICE_JOBS = {}
JOB_LOCK = threading.Lock()
JOB_KEEP = 12


def _new_job() -> dict:
    ident = "%d" % int(time.time() * 1000)
    job = {"id": ident, "stage": "starting", "stages": [], "log": [],
           "layers": 0, "new": [], "bytes": 0, "done": False, "ok": False,
           "error": "", "result": None, "facts": None, "notes": {},
           "started": time.time()}
    with JOB_LOCK:
        SLICE_JOBS[ident] = job
        # Finished jobs are let go, so the app does not grow for ever.
        if len(SLICE_JOBS) > JOB_KEEP:
            for key in list(SLICE_JOBS)[:-JOB_KEEP]:
                SLICE_JOBS.pop(key, None)
    return job


def _stage(job, name, detail=""):
    if not job:
        return
    job["stage"] = name
    job["stages"].append({"name": name, "detail": detail,
                           "at": round(time.time() - job["started"], 1)})


def _log(job, line):
    if not job:
        return
    text = str(line or "").strip()
    if not text:
        return
    job["log"].append(text)
    if len(job["log"]) > 300:
        del job["log"][:-300]


def _run_job(job, model, printer, options):
    try:
        result = slice_model(model, printer, options, job=job)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:200]}
    job["result"] = result
    job["ok"] = bool(result.get("ok"))
    job["facts"] = result.get("facts")
    job["error"] = str(result.get("error") or "")
    job["notes"] = result.get("notes") or {}
    job["done"] = True


def slice_model(model: Path, printer: str, options: dict = None, job=None) -> dict:
    """Slice a model into G-code, with no window open.

    The size of the part and the number of copies are done here, not in
    the slicer: the slicer keeps those on the object and will not take
    them from a profile file. So the model is made the size that was
    asked for and laid out in copies, and the slicer is handed that.
    """
    options = options or {}
    _stage(job, "preparing the model", Path(model).name)
    # A scan of points has no surface to print. Saying so here is kinder
    # than a slicer error ten seconds later.
    try:
        info = wgmesh.mesh_info(model)
    except Exception:
        info = None
    if info and info.get("mode") == 1:
        return {"ok": False,
                "error": "that file is a scan of points, not a surface, so it "
                         "cannot be printed. Export an STL or a 3MF from the "
                         "model first."}
    WORK.mkdir(parents=True, exist_ok=True)
    source = model
    plate = {}
    try:
        recipe = machine_recipe(printer)
        area = recipe.get("area")
        plate = wgmesh.prepare_plate(model, WORK / "plate", options, area,
                                     (options or {}).get("layout"),
                                     recipe.get("height"))
        if plate.get("path"):
            source = Path(plate["path"])
    except Exception as exc:
        # If the person arranged pieces on the bed, printing the model on
        # its own instead would be printing something they did not ask
        # for. So it is said plainly rather than quietly ignored.
        if (options or {}).get("layout"):
            return {"ok": False,
                    "error": "the pieces on the bed could not be laid out: "
                             + str(exc)[:180]}
        source = model
    _stage(job, "the plate is ready", "%d triangle(s) on the bed"
           % int(plate.get("triangles") or 0))
    out_dir = WORK / Path(model).stem

    # The live view. The slicer writes its G-code to a file that grows
    # while it works, and the app reads that file as it grows - so the
    # print is watched being made, which is what a slice with no window
    # open never showed.
    offset = {"path": "", "at": 0}
    streamer = wglayers.Streamer()

    def watch(kind, payload):
        if job is None:
            return
        if kind == "line":
            _log(job, payload)
            return
        path, _size = payload
        path = str(path)
        if offset["path"] != path:
            offset["path"] = path
            offset["at"] = 0
        try:
            with open(path, "rb") as handle:
                handle.seek(offset["at"])
                chunk = handle.read()
        except OSError:
            return
        if not chunk:
            return
        offset["at"] += len(chunk)
        job["bytes"] = offset["at"]
        job["watched"] = int(job.get("watched") or 0) + 1
        for layer in streamer.feed(chunk.decode("utf-8", "replace")):
            segments = layer["segments"]
            if len(segments) > 500:
                step = len(segments) / 500.0
                segments = [segments[int(i * step)] for i in range(500)]
            with JOB_LOCK:
                job["layers"] += 1
                job["new"].append({"z": layer["z"], "segments": segments})

    _stage(job, "the slicer is working", printer)
    try:
        gcode = slice_headless(source, out_dir, printer=printer, options=options,
                               watch=watch)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    if not gcode or not Path(gcode).is_file():
        return {"ok": False,
                "error": "the slicer did not write a G-code file for this model. "
                         "Check that OrcaSlicer is installed and that this "
                         "printer has a profile in it."}
    facts = gcode_facts(gcode)
    # The slicer's own output is kept in the G-code folder, where a person
    # can find it, open it in the slicer and change it. What they save
    # there is what gets printed.
    GCODE.mkdir(parents=True, exist_ok=True)
    kept = GCODE / Path(gcode).name
    try:
        shutil.copyfile(gcode, kept)
    except OSError:
        kept = gcode
    # One line is added to the sliced file so the printer waits for
    # its nozzle before it purges.
    try:
        wait_for_the_nozzle(kept)
    except Exception:
        pass
    with LOCK:
        STATE.update({"gcode": str(kept), "facts": facts,
                      "name": Path(kept).name, "printer": printer,
                      "model": Path(model).name, "source": "sliced here",
                      "slice_time": time.time(), "bed_ready": False})
    _stage(job, "the toolpath is read back",
           "%d layer(s), %s g of filament"
           % (int(facts.get("layers") or 0), facts.get("grams")))
    return {"ok": True, "gcode": str(kept), "name": Path(kept).name,
            "bytes": kept.stat().st_size, "facts": facts,
            "plate": plate, "source": "sliced here",
            "notes": plate.get("notes") or {}}


def refusal_message(result: dict, state: dict = None) -> str:
    """Say what the printer said in words a person can act on.

    The printer answers in its own words, and some of those words are
    instructions for the person standing in front of it.
    """
    message = str(result.get("message") or "").strip()
    lowered = message.lower()
    code = result.get("code")
    what = str((state or {}).get("state") or "").lower()

    # A cancelled job leaves the printer holding the record of it. The
    # record does not clear itself, and no command on the network clears
    # it. The person has to turn the printer off and on again.
    if what == "cancel":
        return ("the printer is still holding a job that was cancelled. "
                "Nothing in this app can clear that - turn the printer off "
                "at the wall and on again, and it will be ready.")

    if "busy" in lowered:
        return ("the printer is busy with another job. Clear it on the "
                "printer itself, or turn the printer off and on again.")

    if code == 1:
        return ("the printer did not accept the access code. Read the "
                "serial number and the code on the printer's screen and "
                "type them in again.")

    # This firmware answers 'Lan mode error' on some calls even when LAN
    # mode is on and the access code is right. It is a stutter in the
    # printer's own network check, so it is worth another try - and only
    # worth blaming the settings if it keeps happening.
    if code == -2 or "lan mode" in lowered:
        return ("the printer did not answer cleanly. It is often the "
                "printer's own network check stuttering, so try again. If "
                "it keeps saying this, check that LAN mode is on in the "
                "printer's network settings.")

    return "the printer refused the file: %s" % (message or "unknown")


def put_on_printer(address: str, serial: str, code: str,
                   path: Path, start: bool = False) -> dict:
    """Upload one file, select it on the printer, and start it if asked.

    The printer names the file it kept, so that is asked for rather than
    guessed - otherwise the job that starts is not the job that was sent.
    """
    # The file is sent with the printer's own "print it now" flag, so
    # the printer starts it by itself. That is the whole point of it: the
    # person never has to walk over to the machine and press its screen.
    result = wgprinter.upload(address, serial, code, path,
                               print_now=start, timeout=180, tries=6)
    if result.get("code") != 0:
        state = wgprinter.printer_status_detail(address, serial, code)
        return {"ok": False, "error": refusal_message(result, state)}
    held = wgprinter.printer_files(address, serial, code)
    landed = ""
    for name in held:
        if name.endswith(path.name):
            landed = name
            break
    if not landed and held:
        landed = held[-1]
    selected = ""
    started = False
    try:
        with wgprinter.Printer(address) as link:
            if start:
                # The printer was asked to start the job as it took the
                # file. Give it a few seconds to do so before anything
                # else is sent, so the job is not started twice.
                deadline = time.time() + 12.0
                while time.time() < deadline:
                    if printer_is_printing(address):
                        started = True
                        break
                    time.sleep(1.0)
            if not started:
                if landed:
                    selected = link.select("0:" + landed if landed.startswith('/')
                                           else "0:/user/" + landed)
                if start:
                    link.start()
                    started = True
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "file": path.name, "on_printer": landed or path.name,
            "bytes": path.stat().st_size, "selected": selected.strip()[:200],
            "started": started,
            "started_by": "the printer itself" if started and not selected
                          else "this app"}


def _job_cleared_note(address: str, serial: str, code: str) -> str:
    """What to tell the person after a job has been stopped.

    The printer accepts a stop command and then, on this firmware, keeps
    holding the record of the job it was given. The record does not clear
    itself and no network command clears it - only turning the printer off
    and on again does. Saying so is more use than "the job was stopped",
    because the next thing the person will try is sending another file,
    and that will be refused with "Printer is busy" and no explanation.
    """
    for _ in range(4):
        state = wgprinter.printer_status_detail(address, serial, code)
        what = str(state.get("state") or "").lower()
        if what in ("ready", "completed"):
            return ""
        if what == "cancel":
            return (" - but the printer is still holding that job. It has "
                    "to be turned off at the wall and on again before it "
                    "will take another file.")
        time.sleep(1.5)
    return ""


# The states the printer uses while a job is actually running.
JOB_RUNNING_STATES = ("PRINTING", "BUILDING_FROM_SD", "BUSY", "HOMING",
                      "HEATING", "PAUSING", "CANCELING", "PAUSE")


def printer_is_printing(address: str) -> bool:
    """True unless the printer says it is idle.

    The printer's own words are the guide. It says READY when it has
    nothing to do, and everything else means it is working on something:
    BUILDING_FROM_SD, PRINTING, HOMING, or WAIT_ON_TOOL, which is the
    printer sitting at its own screen waiting for somebody to confirm the
    filament. Only READY means idle.

    The target temperatures alone are not enough. A job that was
    cancelled has already cooled, but the machine still holds the record
    of it and refuses every file until it is turned off and on again.
    """
    try:
        with wgprinter.Printer(address) as link:
            status = link.status()
            temps = link.temperatures()
    except Exception:
        return False
    machine = str((status or {}).get("machine") or "").upper()
    # BUILDING_COMPLETED means the job is done and the printer is
    # cooling down. That is not a running job, so the prime counts as
    # finished - but the printer still will not take a file for a few
    # minutes, which is what printer_is_ready is for.
    if machine in JOB_RUNNING_STATES:
        return True
    if machine and machine not in ("READY", "IDLE", "BUILDING_COMPLETED",
                                   "COMPLETED"):
        return True
    target = max(float((temps or {}).get("nozzle_target") or 0),
                 float((temps or {}).get("bed_target") or 0))
    return target > 0


def printer_is_ready(address: str) -> bool:
    """True when the printer will take a file right now.

    After a job the printer says "completed" for several minutes while
    the bed cools, and it refuses every upload until it says "ready". So
    a file is not sent into that window - the send waits for it.
    """
    try:
        with wgprinter.Printer(address) as link:
            status = link.status()
            temps = link.temperatures()
    except Exception:
        return False
    machine = str((status or {}).get("machine") or "").upper()
    if machine not in ("READY", "IDLE"):
        return False
    target = max(float((temps or {}).get("nozzle_target") or 0),
                 float((temps or {}).get("bed_target") or 0))
    return target <= 0


def printer_waiting_for_filament(address: str) -> bool:
    """True when the printer is at its screen waiting for the filament.

    The Adventurer asks on its own display for the filament to be
    confirmed before it starts. If that prompt is dismissed instead of
    answered, the printer sits there with the head over the bed and the
    bed hot, and nothing happens. The app has to be able to say that.
    """
    try:
        with wgprinter.Printer(address) as link:
            status = link.status()
    except Exception:
        return False
    move = str((status or {}).get("move") or "").upper()
    return "WAIT_ON_TOOL" in move or "WAIT" in move


def gcode_to_print() -> Path:
    """The app's last preview, never an arbitrary newer file from a folder."""
    return Path(STATE.get("gcode") or "")


def send_to_printer(model_name: str, printer: str, start: bool) -> dict:
    """Legacy clients cannot bypass the visible slicer review/print step."""
    return {"ok": False, "error": "Open the model in the slicer, check the "
            "AD5M profile and layer preview, then print from the slicer. "
            "Print Station does not send or start model jobs."}


def bedprep_state() -> dict:
    """What the bed step needs, and where it stands.

    The prime job is the last thing the printer does before the model, so
    the app watches the printer and closes the step when the prime has run
    to the end. If the prime did not stick, the person sees that before
    the model is sent, not after.
    """
    settings = wgprinter.load_settings()
    address = _address(settings)
    running = False
    if STATE.get("bed_prime") and address:
        running = printer_is_printing(address)
        if running:
            # The prime has been seen running. Only now can it be said
            # to have finished - a printer that has not started the job
            # yet is not printing, and reading that as "the prime ran"
            # would prove the bed with nothing having printed on it.
            with LOCK:
                STATE["bed_prime_started"] = True
        elif STATE.get("bed_prime_started"):
            with LOCK:
                STATE["bed_prime"] = ""
                STATE["bed_prime_started"] = False
                STATE["bed_ready"] = True
    material = wgbed.material_for((STATE.get("facts") or {}).get("filament") or "")
    ticked = STATE.get("bed_checks") or {}
    checks = []
    for check in wgbed.CHECKS:
        item = dict(check)
        item["done"] = bool(ticked.get(check["id"]))
        checks.append(item)
    return {"ok": True, "checks": checks, "material": material,
            "ready": bool(STATE.get("bed_ready")),
            "running": running,
            "prime": STATE.get("bed_prime") or ""}


def prime_the_bed(printer: str) -> dict:
    """Run the adhesion test on the printer, before the real job."""
    settings = wgprinter.load_settings()
    code = settings.get("check_code") or ""
    if not code:
        return {"ok": False,
                "error": "this printer wants its access code once. Type it in "
                         "the printer panel and it is remembered."}
    found = wgprinter.discover(2.5)
    address = _address(settings) or (found[0]["address"] if found else "")
    if not address:
        return {"ok": False, "error": "no printer answered on this network"}
    serial = settings.get("serial") or (found[0]["serial"] if found else "")
    if printer_is_printing(address):
        return {"ok": False, "error": "the printer is already printing."}
    # The prime line is the last step, not the first. The things only a
    # person can do - wipe the bed, put something on it, dry the
    # filament - have to be done and ticked off first.
    checks = wgbed.CHECKS
    ticked = STATE.get("bed_checks") or {}
    missing = [check["id"] for check in checks if not ticked.get(check["id"])]
    if missing:
        return {"ok": False,
                "error": "the bed is not ready yet: %s"
                         % ", ".join(missing)}
    # The prime line runs at the heat the real print will run at, so the
    # test is worth something.
    nozzle, bed = wgbed.temperatures_from_gcode(STATE.get("gcode") or "")
    # The prime is built from the printer's own machine profile, so it
    # heats, homes and purges the way this printer is meant to.
    recipe = machine_recipe(printer)
    start_code = recipe.get("start") or ""
    end_code = recipe.get("end") or ""
    area = recipe.get("area")
    bounds = wgbed.model_bounds(STATE.get("gcode") or "")
    prime = wgbed.write_prime(WORK / "bedprep", nozzle, bed,
                              start_code, end_code, bounds, area)
    result = put_on_printer(address, serial, code, prime, True)
    if not result.get("ok"):
        return result
    with LOCK:
        STATE["bed_prime"] = prime.name
        STATE["bed_prime_started"] = False
        STATE["bed_ready"] = False
    result["nozzle"] = nozzle
    result["bed"] = bed
    result["message"] = "the prime line is on the bed - watch it stick"
    return result


class Server(ThreadingHTTPServer):
    """The app's own web server.

    Windows lets two sockets bind the same port when reuse is on, which
    would leave two copies of the app fighting over one address. It is
    turned off so the second copy steps aside for the first.
    """

    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "MalmarsPrintStation"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ---------------------------------------------------------
    def _send(self, code, payload, ctype="application/json"):
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _local_origin(self) -> bool:
        origin = str(self.headers.get("Origin") or "")
        try:
            parsed = urlparse(origin)
            return (parsed.scheme == "http"
                    and parsed.hostname in ("127.0.0.1", "localhost")
                    and parsed.port == PORT)
        except ValueError:
            return False

    def _body(self, max_bytes: int = MAX_JSON_BYTES) -> bytes:
        value = self.headers.get("Content-Length", "0") or 0
        try:
            length = int(value)
        except (TypeError, ValueError):
            raise ValueError("invalid request length")
        if length < 0:
            raise ValueError("invalid request length")
        if length > max_bytes:
            raise OverflowError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise ValueError("incomplete request body")
        return raw

    def _parse_json(self, raw: bytes) -> dict:
        """The body as a dictionary, if it is one."""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # -- pages ------------------------------------------------------------
    def _index_page(self):
        """Serve the app page, without the Studio link when there is none.

        A standalone copy has no Studio, so it must not offer Studio pages,
        Studio hardware checks or Studio setup. The link is removed on the
        server, so it never flashes up in the top bar before the page
        script has a chance to hide it.
        """
        data = _app_page_html()
        if data is None:
            return self._send(404, {"error": "not found"})
        self._send(200, data.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, name: str):
        target = (WEB / name).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._send(404, {"error": "not found"})
        if not target.is_file():
            return self._send(404, {"error": "not found"})
        kind = "text/plain"
        if target.suffix == ".html":
            kind = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            kind = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            kind = "application/javascript; charset=utf-8"
        elif target.suffix == ".png":
            kind = "image/png"
        elif target.suffix in (".jpg", ".jpeg"):
            kind = "image/jpeg"
        elif target.suffix == ".webp":
            kind = "image/webp"
        elif target.suffix == ".svg":
            kind = "image/svg+xml"
        elif target.suffix == ".ico":
            kind = "image/x-icon"
        self._send(200, target.read_bytes(), kind)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        try:
            _studio_gate(os.environ.get("JL_STUDIO_PRODUCT", "print"))
        except PermissionError as exc:
            return self._json({"ok": False, "locked": True, "error": str(exc)}, 409)
        url = urlparse(self.path)
        route = url.path
        if route in ("/", "/index.html"):
            return self._index_page()
        if route == "/api/app-mode":
            return self._json({
                "ok": True,
                "product": "Malmars Local Print Station",
                "studio": _in_studio(),
            })
        if route.startswith("/") and route.count("/") == 1 and "." in route:
            return self._static(route[1:])

        if route == "/api/health":
            found_slicers = detect_slicers()
            profiles = []
            try:
                profiles = printer_profiles()
            except Exception:
                profiles = []
            return self._json({
                "ok": True,
                "product": "Malmars Local Print Station",
                "version": VERSION,
                "slicers": found_slicers,
                "printer_count": len(profiles),
                "makers": len({item.get("maker", "") for item in profiles
                                if item.get("maker")}),
                "licence": wglicence.load(),
                "state": {"model": STATE.get("model", ""),
                          "name": STATE.get("name", ""),
                          "source": STATE.get("source", ""),
                          "facts": STATE.get("facts") or {},
                          "bed_ready": bool(STATE.get("bed_ready"))},
            })

        if route == "/api/slicers":
            try:
                return self._json({"ok": True, "printers": printer_profiles(),
                                   "slicers": detect_slicers()})
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/slice/progress":
            query = parse_qs(url.query)
            ident = (query.get("job") or [""])[0]
            with JOB_LOCK:
                job = SLICE_JOBS.get(ident)
                if job is None:
                    return self._json({"ok": False,
                                       "error": "that job is not known"}, 404)
                new = job["new"]
                job["new"] = []
            return self._json({"ok": True, "job": ident, "stage": job["stage"],
                               "stages": job["stages"], "log": job["log"][-80:],
                               "layers": job["layers"], "new": new,
                               "watched": job.get("watched") or 0,
                               "bytes": job["bytes"], "done": job["done"],
                               "result": job["result"], "facts": job["facts"],
                               "error": job["error"],
                               "notes": job["notes"],
                               "types": wglayers.TYPES,
                               "colours": wglayers.COLOURS})

        if route == "/api/beds":
            try:
                from slicers import bed_areas
                return self._json({"ok": True, "beds": bed_areas()})
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/printer":
            try:
                return self._json(printer_snapshot())
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/colour/info":
            # What colours a file carries. A print file with the colours
            # in it is the one this app works on, so the page needs to
            # know before it offers the buttons.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            path = MODELS / name
            if not path.is_file():
                return self._json({"error": "that file is not on this PC"}, 404)
            try:
                mesh, slots, colours, palette = wgmesh.load_coloured(path)
            except Exception as exc:
                return self._json({"error": str(exc)[:180]}, 500)
            return self._json({"ok": True, "colours": bool(palette),
                               "materials": len(palette),
                               "faces": mesh.triangles})

        if route == "/api/colour/mesh":
            # The recoloured part, as the block the page draws. Mode 2 is
            # a mesh with a colour on every vertex, so the page can show
            # the processed part next to the original one.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            path = MODELS / name
            if not path.is_file():
                return self._json({"error": "that file is not on this PC"}, 404)
            try:
                mesh, slots, colours, palette = wgmesh.load_coloured(path)
                if not palette:
                    return self._json({"error": "that file has no colours in it"},
                                      400)
                flat = bytearray()
                for rgb in colours:
                    flat.extend((int(rgb[0]) & 255, int(rgb[1]) & 255,
                                 int(rgb[2]) & 255))
                blob = wgcolour.colour_blob(wgmesh.to_view(mesh), flat)
            except Exception as exc:
                return self._json({"error": str(exc)[:180]}, 500)
            return self._send(200, blob, "application/octet-stream")

        if route == "/api/models":
            MODELS.mkdir(parents=True, exist_ok=True)
            out = []
            for path in sorted(MODELS.glob("*")):
                if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES:
                    entry = {"name": path.name, "bytes": path.stat().st_size}
                    # A picture of the model, when one was drawn for it.
                    # The templates that ship with the app carry one, so
                    # the page shows what it is offering instead of a
                    # list of file names.
                    thumb = MODELS / (path.stem + ".png")
                    if thumb.is_file():
                        entry["thumb"] = thumb.name
                    # The picture the model was made from, when there is
                    # one. That is the whole story of this app: a photo
                    # of a plant becomes a part you can print.
                    for suffix in (".png", ".jpg", ".jpeg"):
                        source = MODELS / "sources" / (path.stem + "_source" + suffix)
                        if source.is_file():
                            entry["source_pic"] = source.name
                            break
                    out.append(entry)
            return self._json({"ok": True, "models": out})

        if route == "/api/model/thumb":
            # The picture of a model. It is a picture of the file on
            # this PC and nothing else - no upload, no third party.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            thumb = MODELS / (os.path.splitext(name)[0] + ".png")
            if not thumb.is_file():
                return self._json({"error": "no picture for that model"}, 404)
            return self._send(200, thumb.read_bytes(), "image/png")

        if route == "/api/model/source":
            # The picture a model was made from. It is a picture on this
            # PC and nothing else.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            source = MODELS / "sources" / name
            if not source.is_file():
                return self._json({"error": "no picture for that model"}, 404)
            kind = ("image/jpeg" if source.suffix.lower() in (".jpg", ".jpeg")
                    else "image/png")
            return self._send(200, source.read_bytes(), kind)

        if route == "/api/model/mesh":
            # The model, as a flat block of numbers the page can draw.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            model = MODELS / name
            if not model.is_file():
                return self._json({"error": "that model is not on this PC yet"}, 404)
            try:
                blob = wgmesh.mesh_bytes(model, for_view=True)
            except Exception as exc:
                return self._json({"error": "the model could not be read: %s"
                                            % str(exc)[:160]}, 422)
            return self._send(200, blob, "application/octet-stream")

        if route == "/api/gcode":
            GCODE.mkdir(parents=True, exist_ok=True)
            out = []
            for path in sorted(GCODE.glob("*.gcode")):
                if path.is_file():
                    info = path.stat()
                    out.append({"name": path.name, "bytes": info.st_size,
                                "changed": time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(info.st_mtime))})
            return self._json({"ok": True, "gcode": out, "folder": str(GCODE)})

        if route == "/api/printoptions":
            # What the person can change about the print, and what the
            # slicer has for this printer. Both are read from the
            # slicer itself, so they are never out of date.
            query = parse_qs(url.query)
            printer = (query.get("printer") or [DEFAULT_PRINTER])[0]
            try:
                materials = filaments_for(printer)
            except Exception:
                materials = []
            try:
                recipe = machine_recipe(printer)
            except Exception:
                recipe = {}
            return self._json({"ok": True,
                               "options": PRINT_OPTIONS,
                               "default_filament": default_filament(printer),
                               "materials": materials,
                               # Where the bed really is, so the page can draw
                               # it and put the pieces on it.
                               "bed_area": recipe.get("area"),
                               # How tall this printer goes. The bed on
                               # its own is not the whole story - a part
                               # that is too tall for the machine cannot
                               # be printed on it.
                               "bed_height": recipe.get("height")})

        if route == "/api/gcode/layers":
            # What the slicer made: how many layers, how many moves of
            # each kind, the settings it used, and where the model sits
            # on the bed. The bed itself comes from the machine profile
            # the slicer used, so the view is the printer's own bed and
            # not a guess - including the machines whose origin is in
            # the middle of the bed.
            gcode = gcode_to_print()
            if not gcode.is_file():
                return self._json({"error": "there is no sliced file yet"}, 404)
            query = parse_qs(url.query)
            printer = (query.get("printer") or [DEFAULT_PRINTER])[0]
            area = None
            try:
                area = machine_recipe(printer).get("area")
            except Exception:
                area = None
            try:
                return self._json({"ok": True,
                                   "area": list(area) if area else None,
                                   **wglayers.summary(gcode)})
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/gcode/layer":
            # One layer at a time. The whole file is nearly fifteen
            # megabytes of coordinates and nobody needs all of it at
            # once, so the page asks for the layer it is showing.
            query = parse_qs(url.query)
            index = (query.get("index") or ["0"])[0]
            gcode = gcode_to_print()
            if not gcode.is_file():
                return self._json({"error": "there is no sliced file yet"}, 404)
            try:
                return self._json(wglayers.layer(gcode, index))
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/bedprep":
            return self._json(bedprep_state())

        if route == "/api/licence":
            return self._json({"ok": True, "licence": wglicence.load()})

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            _studio_gate(os.environ.get("JL_STUDIO_PRODUCT", "print"))
        except PermissionError as exc:
            return self._json({"ok": False, "locked": True, "error": str(exc)}, 409)
        url = urlparse(self.path)
        route = url.path
        uploads = {
            "/api/model": ("application/octet-stream", MAX_MODEL_UPLOAD_BYTES),
            "/api/model/source": ("application/octet-stream",
                                  MAX_SOURCE_UPLOAD_BYTES),
        }
        upload = uploads.get(route)
        origin = str(self.headers.get("Origin") or "")
        if (not upload and not self._local_origin()) or (
                upload and origin and not self._local_origin()):
            return self._json({"error": "this request must come from Print Station"},
                              403)
        expected_type, limit = upload or ("application/json", MAX_JSON_BYTES)
        content_type = str(self.headers.get("Content-Type") or "")
        content_type = content_type.split(";", 1)[0].strip().lower()
        accepted_types = {expected_type}
        # Older running World Generator builds use urllib's form default.
        # They can upload only from a local process (no browser Origin).
        if upload and not origin:
            accepted_types.add("application/x-www-form-urlencoded")
        if content_type not in accepted_types:
            return self._json({"error": "unsupported request content type"}, 415)
        # The body is read exactly once. A file upload comes through here
        # too, and reading it twice would wait for bytes that never come.
        try:
            raw = self._body(limit)
        except OverflowError:
            return self._json({"error": "request body exceeds the size limit"}, 413)
        except ValueError:
            return self._json({"error": "invalid request body"}, 400)
        body = self._parse_json(raw)

        if route == "/api/model":
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            if not name or not name.lower().endswith(MODEL_SUFFIXES):
                return self._json({"error": "that is not a model file this app "
                                            "can print (.stl, .3mf, .obj, .ply, "
                                            ".glb)"}, 400)
            if not raw:
                return self._json({"error": "the file arrived empty"}, 400)
            MODELS.mkdir(parents=True, exist_ok=True)
            (MODELS / name).write_bytes(raw)
            return self._json({"ok": True, "name": name, "bytes": len(raw)})

        if route == "/api/model/source":
            # The picture the model was made from. Another app on this PC
            # hands over the model and the photo it came from together, so
            # the desk shows the whole story: the picture on one side, the
            # model that came out of it on the other.
            query = parse_qs(url.query)
            name = os.path.basename(unquote((query.get("name") or [""])[0]))[:120]
            if not name or not name.lower().endswith((".png", ".jpg", ".jpeg")):
                return self._json({"error": "that is not a picture this app "
                                            "can show"}, 400)
            if not raw:
                return self._json({"error": "the picture arrived empty"}, 400)
            folder = MODELS / "sources"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(raw)
            return self._json({"ok": True, "name": name, "bytes": len(raw)})

        if route == "/api/colour/plan":
            # The vectorise. The colours in the file are reduced to the
            # number the printer can lay down, the speckle is taken off,
            # and the result is written out as a 3MF with the colour on
            # every face - which is the file the slicer wants.
            name = os.path.basename(str(body.get("model") or ""))[:120]
            path = MODELS / name
            if not path.is_file():
                return self._json({"error": "that file is not on this PC"}, 404)
            try:
                count = int(body.get("colours") or 4)
            except (TypeError, ValueError):
                count = 4
            try:
                noise = int(body.get("noise") or 1)
            except (TypeError, ValueError):
                noise = 1
            try:
                mesh, slots, colours, palette = wgmesh.load_coloured(path)
                if not palette:
                    return self._json({"error": "that file has no colours in it. "
                                                "The vectoriser works on a print "
                                                "file that carries its own "
                                                "materials."}, 400)
                result = wgcolour.vectorise(mesh, colours, count, noise)
                if not result["palette"]:
                    return self._json({"error": "the colours in that file could "
                                                "not be read"}, 400)
                out = MODELS / (Path(path).stem + "_vector.3mf")
                wgcolour.write_3mf([{"mesh": mesh, "slots": result["slots"]}],
                                    result["palette"], out)
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)
            return self._json({"ok": True,
                               "file": out.name,
                               "faces": result["faces"],
                               "palette": [{"name": entry["name"],
                                            "hex": entry["hex"],
                                            "share": entry["share"],
                                            "distance": entry["distance"]}
                                           for entry in result["palette"]]})

        if route == "/api/printer/choose":
            # Use another printer on this network. The address is kept on
            # this PC, the same way the access code is.
            address = str(body.get("address") or "").strip()
            serial = str(body.get("serial") or "").strip()
            if not address:
                return self._json({"ok": False,
                                   "error": "no printer was named"}, 400)
            wgprinter.save_settings({"address": address, "serial": serial})
            return self._json({"ok": True, "address": address})

        if route == "/api/slice":
            name = os.path.basename(str(body.get("model") or ""))[:120]
            printer = str(body.get("printer") or "").strip()
            model = MODELS / name
            if not model.is_file():
                return self._json({"error": "that model is not on this PC yet"}, 404)
            options = body.get("options")
            if not isinstance(options, dict):
                options = {}
            # The slice runs in the background so the page can watch it.
            job = _new_job()
            worker = threading.Thread(target=_run_job,
                                      args=(job, model, printer, options))
            worker.daemon = True
            worker.start()
            return self._json({"ok": True, "job": job["id"]})

        if route == "/api/gcode/open":
            # Show the person where the slicer's G-code lives.
            GCODE.mkdir(parents=True, exist_ok=True)
            try:
                if hasattr(os, "startfile"):
                    os.startfile(str(GCODE))  # noqa: S606
            except Exception as exc:
                return self._json({"error": "the folder could not be opened: %s"
                                            % str(exc)[:120]}, 500)
            return self._json({"ok": True, "folder": str(GCODE)})

        if route == "/api/bedprep/check":
            # One of the bed checks, ticked or unticked by the person.
            check = str(body.get("id") or "").strip()
            known = [item["id"] for item in wgbed.CHECKS]
            if check not in known:
                return self._json({"error": "that is not one of the checks"}, 400)
            with LOCK:
                STATE.setdefault("bed_checks", {})[check] = bool(body.get("done"))
            return self._json(bedprep_state())

        if route == "/api/bedprep":
            return self._json(prime_the_bed(str(body.get("printer") or "")))

        if route == "/api/handoff":
            # Open the model in the slicer's own window, with the machine
            # already chosen. Sometimes a person wants to see the layers
            # before the money and the time are spent.
            name = os.path.basename(str(body.get("model") or ""))[:120]
            printer = str(body.get("printer") or "").strip()
            model = MODELS / name
            if not model.is_file():
                return self._json({"error": "that model is not on this PC yet"}, 404)
            opened = open_in_slicer(model, printer)
            return self._json({"ok": bool(opened),
                               "error": "" if opened else "no slicer window could be opened"})

        if route == "/api/printer/code":
            code = str(body.get("check_code") or "").strip()
            if not code:
                return self._json({"error": "no access code given"}, 400)
            wgprinter.save_settings({"check_code": code})
            return self._json({"ok": True})

        if route == "/api/printer/send":
            name = os.path.basename(str(body.get("model") or ""))[:120]
            printer = str(body.get("printer") or "").strip()
            start = bool(body.get("start"))
            try:
                return self._json(send_to_printer(name, printer, start), 409)
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 500)

        if route == "/api/printer/confirm":
            # Answer the question on the printer's own screen from here.
            settings = wgprinter.load_settings()
            found = wgprinter.discover(2.5)
            address = _address(settings) or (found[0]["address"] if found else "")
            if not address:
                return self._json({"error": "no printer answered on this network"}, 404)
            serial = settings.get("serial") or (found[0]["serial"] if found else "")
            answer = wgprinter.confirm_filament(
                address, serial, settings.get("check_code") or "")
            with LOCK:
                STATE["last_prompt"] = (
                    "the printer asked for the filament and the app answered it"
                    if answer.get("ok") else
                    "the printer asked for the filament and the app could not "
                    "answer it")
            return self._json(answer)

        if route == "/api/printer/control":
            action = str(body.get("action") or "").strip().lower()
            if action == "start":
                return self._json({"error": "Start reviewed jobs from the slicer "
                                           "or the printer screen."}, 409)
            settings = wgprinter.load_settings()
            found = wgprinter.discover(2.5)
            address = _address(settings) or (found[0]["address"] if found else "")
            if not address:
                return self._json({"error": "no printer answered on this network"}, 404)
            serial = settings.get("serial") or (found[0]["serial"] if found else "")
            if action not in ("pause", "resume", "stop"):
                return self._json({"error": "unknown action"}, 400)
            # The printer's own control API is tried first, because it is
            # the one that really clears a job that has gone wrong. The
            # plain TCP commands are the fallback.
            wanted = {"pause": "pause", "resume": "continue", "stop": "cancel"}
            answer = wgprinter.job_control(address, serial, settings.get("check_code") or "",
                                           wanted[action])
            reply = str(answer.get("message") or "")
            if answer.get("code") == 0:
                # The printer says it accepted the command. That is not the
                # same as the job being gone. On this firmware a job that
                # stopped badly leaves the printer holding the record of
                # it, and every upload is refused with "Printer is busy"
                # until the printer is turned off and on again. So the
                # answer is checked before it is given, and the truth is
                # told.
                note = ""
                if action == "stop":
                    note = _job_cleared_note(address, serial,
                                            settings.get("check_code") or "")
                return self._json({"ok": True, "action": action,
                                   "reply": (reply.strip()[:200] + note)})
            try:
                with wgprinter.Printer(address) as link:
                    if action == "pause":
                        reply = link.pause()
                    elif action == "resume":
                        reply = link.resume()
                    else:
                        reply = link.stop()
            except Exception as exc:
                return self._json({"error": str(exc)[:200]}, 502)
            note = ""
            if "lan mode" in str(answer.get("message") or "").lower():
                note = (" (the printer is not in LAN mode, so it will not "
                        "take network commands)")
            return self._json({"ok": True, "action": action,
                               "reply": (reply.strip()[:200] + note)})

        if route == "/api/quit":
            # Shut the app down. There is no window to close, so this is
            # how a person stops it.
            def stop():
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=stop, daemon=True).start()
            return self._json({"ok": True, "message": "the print station is closing"})

        if route == "/api/licence":
            code = str(body.get("code") or "").strip()
            result = wglicence.save(code)
            return self._json({"ok": bool(result.get("licensed")),
                               "licence": result})

        return self._json({"error": "not found"}, 404)

    def _json(self, payload, code=200):
        self._send(code, payload)


def _serve():
    MODELS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    url = "http://%s:%d/" % (HOST, PORT)
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        # The port is taken. That almost always means the app is already
        # running, so open it rather than showing an error to a person who
        # only wanted to look at their printer.
        try:
            (DATA / "print-station.log").write_text(
                "%s - port %d already in use, opening the running app\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), PORT), encoding="utf-8")
        except Exception:
            pass
        if not os.environ.get("PRINT_STATION_NO_BROWSER"):
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return
    print("MALMARS LOCAL PRINT STATION")
    print("  open      : %s" % url)
    print("  models    : %s" % MODELS)
    print("  licence   : %s" % ("licensed to " + wglicence.load().get("licencee", "")
                                if wglicence.load().get("licensed") else "none on this install"))
    print("  stop with : ctrl+c")
    if not os.environ.get("PRINT_STATION_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()


def main():
    """Start the app, and say so plainly if it cannot start."""
    try:
        _studio_gate("print")
    except PermissionError as exc:
        print("[print-station] %s" % exc)
        return
    try:
        _serve()
    except Exception:
        detail = traceback.format_exc()
        try:
            (DATA / "startup-error.log").write_text(detail, encoding="utf-8")
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, detail[-1400:],
                "Malmars Local Print Station could not start", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
