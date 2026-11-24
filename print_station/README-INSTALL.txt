MALMARS LOCAL PRINT STATION — FREE BETA
==================================

WHAT THIS APP DOES
------------------
Print Station is a free, local companion for reviewing a model before a
print. It does not send or start model jobs. Open the model in the visible
slicer, check its bed placement, settings and layer preview there, then
choose whether to print from the slicer or printer.

The app can also make an optional local estimate. That estimate is not a
print job and is never sent to the printer.

WHAT YOU NEED
-------------
The EXE is self-contained; Python does not need to be installed.

Install a slicer separately:
  - Flashforge Studio is preferred for the Flashforge Adventurer 5M.
  - OrcaSlicer is used as a fallback if Flashforge Studio is not installed.

Neither slicer is included in this package. Install it from its publisher.
The app opens the model in a visible slicer window and does not replace or
silently override the slicer's own profiles.

START
-----
Double-click START_PRINT_STATION.cmd or Malmars Local Print Station.exe. The app
opens at http://127.0.0.1:8779/ on this PC only.

WORKFLOW
--------
1. Open a model in Print Station and inspect it in the 3D view.
2. Choose “Open model in the slicer”. For an AD5M, Flashforge Studio is
   preferred when available. Inspect the real model and layer preview.
3. Make any changes and explicitly slice/print using the slicer's controls.

The optional “Estimate here” action runs a local slicer estimate only. It
does not upload a model or G-code to the printer.

“Prime the bed” is a separate, explicit hardware action. After the person
checks the bed and filament checklist, it sends a short prime line over the
local network. The app can also expose explicit pause, resume and stop
controls for a job already on the printer. It never sends or starts a model
job.

LOCAL DATA AND PRIVACY
----------------------
There is no account, cloud service, telemetry or internet upload. Models and
settings stay on this PC. The printer is contacted on the local network.

The app stores its models, estimates, settings and optional printer access
code beside the EXE when that folder is writable; otherwise it uses
%LOCALAPPDATA%\Malmars\PrintStation. Keep that data folder private. Do not
copy it into a release package or share it: it may contain models, printer
credentials and personal source images.

OPTIONAL SUPPORT
----------------
The app is free to use without a code. A signed support code, when issued,
only identifies a support entitlement; it does not unlock printing
features. See LICENCE-AGREEMENT.txt for the free beta terms.

TROUBLESHOOTING
---------------
“The app is already running”
    Use the browser tab at http://127.0.0.1:8779/. Only one instance can use
    that port at a time.

“No slicer window opened”
    Install Flashforge Studio for the AD5M, or install OrcaSlicer as a
    fallback, then try again.

“The printer did not answer”
    Check that the PC and printer are on the same local network and that the
    printer's LAN mode is enabled.

Test a small print before a long one. Follow the printer and filament
manufacturers' instructions for temperature, bed preparation and safety.

YOUR PRINTER, ON THIS MACHINE
-----------------------------
The printer is added once per machine, by you, and it is not part of this
package. The app finds printers on your local network by asking, never by
guessing, and some printers also need the access code shown on the printer
screen. Nothing about your printer, your access code or your models leaves
this PC.
